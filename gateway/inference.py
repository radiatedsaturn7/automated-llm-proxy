import asyncio
import json
import math
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Histogram
from sqlalchemy import select

from . import db, guardrails, policy, provider_health
from .config import get_settings
from .providers import StreamTranslator, response_headers as provider_response_headers, translate_response, upstream_request
from .security import inference_identity

router = APIRouter()
log = logging.getLogger("gateway")
REQUESTS = Counter("gateway_inference_total", "Inference requests", ["endpoint", "outcome"])
LATENCY = Histogram("gateway_upstream_seconds", "Upstream connection latency", ["endpoint"])


def validate_payload(payload, endpoint):
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON object required")
    allowed = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "temperature", "top_p",
               "stop", "seed", "tools", "tool_choice", "response_format", "presence_penalty", "frequency_penalty",
               "parallel_tool_calls", "reasoning_effort", "stream_options"}
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
            if not isinstance(m, dict) or not isinstance(m.get("role"), str) or m.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
                raise HTTPException(400, "Invalid message")
            if set(m) - {"role", "content", "name", "tool_calls", "tool_call_id"}:
                raise HTTPException(400, "Unsupported message fields")
            calls = m.get("tool_calls")
            if calls is not None:
                if m["role"] != "assistant" or not isinstance(calls, list) or not 1 <= len(calls) <= 128:
                    raise HTTPException(400, "Invalid assistant tool calls")
                for call in calls:
                    if (not isinstance(call, dict) or set(call) - {"id", "type", "function"}
                            or call.get("type") != "function" or not isinstance(call.get("id"), str)
                            or not 1 <= len(call["id"]) <= 256 or not isinstance(call.get("function"), dict)):
                        raise HTTPException(400, "Invalid function tool call")
                    fn = call["function"]
                    if (set(fn) - {"name", "arguments"} or not isinstance(fn.get("name"), str)
                            or not 1 <= len(fn["name"]) <= 128 or not isinstance(fn.get("arguments"), str)):
                        raise HTTPException(400, "Invalid function tool call")
            if not isinstance(m.get("content"), str) and not (m["role"] == "assistant" and calls and m.get("content") is None):
                raise HTTPException(400, "Text content or assistant tool calls required")
            if m["role"] == "tool":
                if not isinstance(m.get("tool_call_id"), str) or not 1 <= len(m["tool_call_id"]) <= 256:
                    raise HTTPException(400, "Tool message needs tool_call_id")
            elif "tool_call_id" in m:
                raise HTTPException(400, "tool_call_id is only valid for tool messages")
            if "name" in m and (not isinstance(m["name"], str) or not 1 <= len(m["name"]) <= 128):
                raise HTTPException(400, "Invalid message name")
        if "stream_options" in payload:
            opts = payload["stream_options"]
            if (not payload.get("stream") or not isinstance(opts, dict) or set(opts) - {"include_usage"}
                    or ("include_usage" in opts and not isinstance(opts["include_usage"], bool))):
                raise HTTPException(400, "Invalid stream_options")
        if "stream" in payload and not isinstance(payload["stream"], bool):
            raise HTTPException(400, "stream must be boolean")
        if "max_tokens" in payload and "max_completion_tokens" in payload:
            raise HTTPException(400, "Choose one completion limit")
        count = payload.get("max_tokens", payload.get("max_completion_tokens", 512))
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= get_settings().max_output_tokens:
            raise HTTPException(400, "Completion token limit invalid")
        if "max_completion_tokens" not in payload:
            payload["max_tokens"] = count
        if "parallel_tool_calls" in payload and not isinstance(payload["parallel_tool_calls"], bool):
            raise HTTPException(400, "parallel_tool_calls must be boolean")
        if "reasoning_effort" in payload and (not isinstance(payload["reasoning_effort"], str) or payload["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh"}):
            raise HTTPException(400, "Invalid reasoning_effort")
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
    identity = await inference_identity(request)
    async with db.session_factory() as s:
        providers = (await s.scalars(select(db.Provider).where(db.Provider.enabled.is_(True)))).all()
    aliases = sorted({alias for p in providers for alias in p.models
                      if not identity.key.allowed_models or alias in identity.key.allowed_models})
    return {"object": "list", "data": [{"id": a, "object": "model", "owned_by": "gateway"} for a in aliases]}


async def finish_request(request, response, reservation_id, model, provider, route, usage=None, release=False, health_ticket=None, outcome=None, retry_after=None):
    # Schedule accounting before the first await: ASGI disconnect cancellation must
    # not strand it behind an interrupted upstream aclose(). Crash recovery still
    # retains a conservative pending reservation if the process dies entirely.
    async def finish():
        try:
            await policy.finalize(reservation_id, model, provider.id, route, usage, release=release)
        finally:
            if outcome is not None:
                try:
                    await provider_health.record(health_ticket, outcome, retry_after)
                except Exception:
                    log.error("Provider health update failed")
            if response is not None:
                try:
                    await asyncio.wait_for(response.aclose(), timeout=5)
                except (httpx.HTTPError, TimeoutError):
                    log.warning("Provider response cleanup failed")

    task = asyncio.create_task(finish())
    request.app.state.accounting_tasks.add(task)

    def done(completed):
        request.app.state.accounting_tasks.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            log.error("Inference accounting failed; reservation requires reconciliation")

    task.add_done_callback(done)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The retained task runs outside the cancelled streaming/request scope.
        pass


def provider_json(raw):
    """Reject ambiguous or non-finite provider JSON before normalization."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate provider JSON key")
            result[key] = value
        return result

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite provider JSON number")
        return parsed

    def invalid(value):
        raise ValueError("Invalid provider JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=invalid)


def response_headers(response, request_id):
    headers = provider_response_headers(response, request_id)
    # Correlation IDs originate upstream and can carry uninspected content too.
    if get_settings().guardrails_output_enabled:
        headers.pop("X-Provider-Request-ID", None)
    return headers


async def infer(request, endpoint):
    identity = await inference_identity(request)
    try:
        payload = validate_payload(await request.json(), endpoint)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if get_settings().guardrails_output_enabled and payload.get("stream"):
        raise HTTPException(400, "Streaming is unavailable with output guardrails enabled")
    if (get_settings().guardrails_output_enabled and endpoint == "embeddings"
            and payload.get("encoding_format", "float") != "float"):
        raise HTTPException(400, "Output guardrails require float embeddings")
    model = payload["model"]
    if identity.key.allowed_models and model not in identity.key.allowed_models:
        raise HTTPException(403, "Model not allowed")
    try:
        guarded = guardrails.preflight(payload, "embeddings" if endpoint == "embeddings" else "chat", get_settings())
    except guardrails.GuardrailRejected as rejected:
        async with db.transaction(inference=True) as session:
            await db.audit(session, identity.user.id, "guardrail.block", reason=rejected.reason,
                           detector_counts=rejected.counts)
        raise HTTPException(403, "Inbound content rejected by gateway policy") from None
    payload = guarded.payload
    if guarded.counts:
        async with db.transaction(inference=True) as session:
            await db.audit(session, identity.user.id, "guardrail.redact", detector_counts=guarded.counts)
    # Choose one healthy route before reservation/dispatch. Never retry this request.
    provider, health_ticket = await provider_health.choose(model, endpoint)
    route = provider.models[model]
    try:
        url, headers, body = upstream_request(provider, route, payload, endpoint)
        input_bound = policy.token_bound(payload)
        output_bound = payload.get("max_tokens", payload.get("max_completion_tokens", 0))
        amount = policy.price(input_bound * 2, output_bound * 2, route)
        reservation_id = await policy.reserve(identity, amount, input_bound + output_bound, model)
    except BaseException:
        await provider_health.abandon(health_ticket)
        raise
    client = request.app.state.upstream
    response = None
    try:
        with LATENCY.labels(endpoint).time():
            response = await client.send(client.build_request("POST", url, headers=headers, json=body), stream=True)
        if response.status_code >= 300:
            # Provider may have processed 5xx responses; keep conservative charge. Definitive 4xx release.
            await finish_request(request, response, reservation_id, model, provider, route,
                                 release=400 <= response.status_code < 500, health_ticket=health_ticket,
                                 outcome=provider_health.classify(response.status_code),
                                 retry_after=response.headers.get("retry-after"))
            REQUESTS.labels(endpoint, "upstream_error").inc()
            raise HTTPException(429 if response.status_code == 429 else 502, "Provider rejected request",
                                headers=response_headers(response, reservation_id))
        if payload.get("stream"):
            translator = StreamTranslator(provider, model)

            async def stream():
                completed = False
                health_outcome = None
                try:
                    async for chunk in translator.iterate(response):
                        yield chunk
                    completed = translator.complete
                    health_outcome = 'success' if completed else 'failure'
                    REQUESTS.labels(endpoint, "success" if completed else "stream_error").inc()
                except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                    health_outcome = 'failure'
                    REQUESTS.labels(endpoint, "stream_error").inc()
                    yield b'data: {"error":{"message":"Upstream stream interrupted","type":"upstream_error"}}\n\n'
                finally:
                    await finish_request(request, response, reservation_id, model, provider, route,
                                         translator.usage if completed else None, health_ticket=health_ticket, outcome=health_outcome)

            return StreamingResponse(stream(), media_type="text/event-stream", headers={
                **response_headers(response, reservation_id), "X-Accel-Buffering": "no"})
        chunks = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 16_777_216:
                raise ValueError("Provider response exceeded size limit")
            chunks.append(chunk)
        await response.aclose()
        data = provider_json(b"".join(chunks))
        if not isinstance(data, dict):
            raise ValueError("Provider response not an object")
        data = translate_response(provider, data, model, endpoint=endpoint, payload=payload)
        await finish_request(request, response, reservation_id, model, provider, route, data.get("usage"),
                             health_ticket=health_ticket, outcome='success')
        try:
            guarded_output = guardrails.postflight(data, get_settings(), endpoint="embeddings" if endpoint == "embeddings" else "chat")
        except guardrails.GuardrailRejected as rejected:
            async with db.transaction(inference=True) as session:
                await db.audit(session, identity.user.id, "guardrail.output_block",
                               reason=rejected.reason, detector_counts=rejected.counts)
            REQUESTS.labels(endpoint, "output_blocked").inc()
            raise HTTPException(403, "Output rejected by gateway policy",
                                headers=response_headers(response, reservation_id)) from None
        data = guarded_output.payload
        if guarded_output.counts:
            async with db.transaction(inference=True) as session:
                await db.audit(session, identity.user.id, "guardrail.output_redact",
                               detector_counts=guarded_output.counts)
        REQUESTS.labels(endpoint, "success").inc()
        return JSONResponse(data, headers=response_headers(response, reservation_id))
    except HTTPException:
        raise
    except asyncio.CancelledError:
        await finish_request(request, response, reservation_id, model, provider, route)
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        await finish_request(request, response, reservation_id, model, provider, route,
                             health_ticket=health_ticket, outcome='failure')
        REQUESTS.labels(endpoint, "error").inc()
        raise HTTPException(502, "Provider unavailable or invalid response", headers=response_headers(response, reservation_id) if response is not None else {"X-Reservation-ID": reservation_id}) from None


@router.post("/v1/chat/completions")
async def chat(request: Request):
    return await infer(request, "chat/completions")


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    return await infer(request, "embeddings")
