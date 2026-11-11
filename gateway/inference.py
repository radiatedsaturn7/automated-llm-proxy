import asyncio
import json
import logging
import math

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Histogram
from sqlalchemy import select

from . import db, policy
from .config import get_settings
from .providers import StreamTranslator, translate_response, upstream_request
from .security import identify

router = APIRouter()
log = logging.getLogger("gateway")
REQUESTS = Counter("gateway_inference_total", "Inference requests", ["endpoint", "outcome"])
LATENCY = Histogram("gateway_upstream_seconds", "Upstream connection latency", ["endpoint"])


def validate_payload(payload, endpoint):
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON object required")
    allowed = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "temperature", "top_p",
               "stop", "seed", "tools", "tool_choice", "response_format", "presence_penalty", "frequency_penalty"}
    if endpoint == "embeddings":
        allowed = {"model", "input", "encoding_format", "dimensions"}
    if set(payload) - allowed:
        raise HTTPException(400, "Unsupported request fields: " + ", ".join(sorted(set(payload) - allowed)))
    if not isinstance(payload.get("model"), str) or not 1 <= len(payload["model"]) <= 128:
        raise HTTPException(400, "Model alias required")
    if endpoint == "chat/completions":
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 1000:
            raise HTTPException(400, "Messages required")
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
                raise HTTPException(400, "Invalid message")
            if not isinstance(m.get("content"), str):
                raise HTTPException(400, "Only text message content supported in this release")
        if "stream" in payload and not isinstance(payload["stream"], bool):
            raise HTTPException(400, "stream must be boolean")
        if "max_tokens" in payload and "max_completion_tokens" in payload:
            raise HTTPException(400, "Choose one completion limit")
        count = payload.get("max_tokens", payload.get("max_completion_tokens", 512))
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= get_settings().max_output_tokens:
            raise HTTPException(400, "Completion token limit invalid")
        payload["max_tokens"] = count
        payload.pop("max_completion_tokens", None)
        for field in ["temperature", "top_p", "presence_penalty", "frequency_penalty"]:
            if field in payload and (isinstance(payload[field], bool) or not isinstance(payload[field], (float, int)) or not math.isfinite(payload[field])):
                raise HTTPException(400, "Invalid numeric parameter")
    else:
        inp = payload.get("input")
        if not isinstance(inp, str) and not (isinstance(inp, list) and 1 <= len(inp) <= 1000 and all(isinstance(x, str) for x in inp)):
            raise HTTPException(400, "Embeddings input must be text or list of text")
    return payload


@router.get("/v1/models")
async def models(request: Request):
    identity = await identify(request)
    async with db.session_factory() as s:
        providers = (await s.scalars(select(db.Provider).where(db.Provider.enabled.is_(True)))).all()
    aliases = sorted({alias for p in providers for alias in p.models
                      if not identity.key.allowed_models or alias in identity.key.allowed_models})
    return {"object": "list", "data": [{"id": a, "object": "model", "owned_by": "gateway"} for a in aliases]}


async def infer(request, endpoint):
    identity = await identify(request)
    try:
        payload = validate_payload(await request.json(), endpoint)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    model = payload["model"]
    if identity.key.allowed_models and model not in identity.key.allowed_models:
        raise HTTPException(403, "Model not allowed")
    async with db.session_factory() as s:
        providers = (await s.scalars(select(db.Provider).where(db.Provider.enabled.is_(True)).order_by(
            db.Provider.priority, db.Provider.id))).all()
    # Explicit deterministic routing. No automatic inference retries: avoids duplicate billing.
    provider = next((p for p in providers if model in p.models and (endpoint != "embeddings" or p.kind == "openai")), None)
    if not provider:
        raise HTTPException(404, "Configured model not available")
    route = provider.models[model]
    url, headers, body = upstream_request(provider, route, payload, endpoint)
    input_bound = policy.token_bound(payload)
    output_bound = payload.get("max_tokens", 0)
    amount = policy.price(input_bound * 2, output_bound * 2, route)
    reservation_id = await policy.reserve(identity, amount, input_bound + output_bound, model)
    client = request.app.state.upstream
    response = None
    try:
        with LATENCY.labels(endpoint).time():
            response = await client.send(client.build_request("POST", url, headers=headers, json=body), stream=True)
        if response.status_code >= 300:
            await response.aclose()
            # Provider may have processed 5xx responses; keep conservative charge. Definitive 4xx release.
            await policy.finalize(reservation_id, model, provider.id, route, release=400 <= response.status_code < 500)
            REQUESTS.labels(endpoint, "upstream_error").inc()
            raise HTTPException(429 if response.status_code == 429 else 502, "Provider rejected request",
                                headers={"X-Request-ID": reservation_id})
        if payload.get("stream"):
            translator = StreamTranslator(provider, model)

            async def stream():
                completed = False
                try:
                    async for chunk in translator.iterate(response):
                        yield chunk
                    completed = translator.complete
                    REQUESTS.labels(endpoint, "success").inc()
                except (httpx.HTTPError, ValueError):
                    REQUESTS.labels(endpoint, "stream_error").inc()
                    yield b'data: {"error":{"message":"Upstream stream interrupted","type":"upstream_error"}}\n\n'
                finally:
                    await response.aclose()
                    # Shield accounting on client cancellation. A process crash leaves the full reservation charged.
                    task = asyncio.create_task(policy.finalize(reservation_id, model, provider.id, route, translator.usage if completed else None))
                    request.app.state.accounting_tasks.add(task)
                    task.add_done_callback(request.app.state.accounting_tasks.discard)
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass

            return StreamingResponse(stream(), media_type="text/event-stream", headers={
                "Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Request-ID": reservation_id})
        chunks = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 16_777_216:
                raise ValueError("Provider response exceeded size limit")
            chunks.append(chunk)
        await response.aclose()
        data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise ValueError("Provider response not an object")
        data = translate_response(provider, data, model)
        await policy.finalize(reservation_id, model, provider.id, route, data.get("usage"))
        REQUESTS.labels(endpoint, "success").inc()
        return JSONResponse(data, headers={"X-Request-ID": reservation_id, "Cache-Control": "no-store"})
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError):
        if response:
            await response.aclose()
        await policy.finalize(reservation_id, model, provider.id, route)
        REQUESTS.labels(endpoint, "error").inc()
        raise HTTPException(502, "Provider unavailable or invalid response", headers={"X-Request-ID": reservation_id}) from None


@router.post("/v1/chat/completions")
async def chat(request: Request):
    return await infer(request, "chat/completions")


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    return await infer(request, "embeddings")
