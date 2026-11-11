import asyncio
import ipaddress
import json
import socket
import re
from urllib.parse import urlparse

from fastapi import HTTPException

from .config import get_settings
from .security import decrypt


async def validate_base_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(400, "Provider base_url must be HTTPS without credentials, query or fragment")
    if get_settings().allow_private_provider_hosts:
        return
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HTTPException(400, "Provider host cannot be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise HTTPException(400, "Private provider hosts are disabled")
    # DNS checks are defense-in-depth; deployment egress policy must prevent rebinding/metadata access.


def upstream_request(provider, route, payload, endpoint):
    body = dict(payload)
    body["model"] = route["upstream_model"]
    if provider.kind in {"openai", "azure_openai"}:
        headers = {"Content-Type": "application/json"}
        credential = decrypt(provider.encrypted_key)
        if provider.kind == "azure_openai":
            # Azure OpenAI v1: base_url ends /openai/v1; model is deployment name.
            headers["api-key"] = credential
        else:
            headers["Authorization"] = "Bearer " + credential
        if body.get("stream"):
            body["stream_options"] = {"include_usage": True}
        return provider.base_url + "/" + endpoint, headers, body
    if provider.kind != "anthropic":
        raise HTTPException(400, "Unsupported provider adapter")
    if endpoint != "chat/completions":
        raise HTTPException(400, "This provider supports chat only")
    unsupported = {"tools", "tool_choice", "response_format", "seed", "presence_penalty", "frequency_penalty", "parallel_tool_calls", "max_completion_tokens", "reasoning_effort", "stream_options"}
    if set(payload) & unsupported:
        raise HTTPException(400, "Unsupported Anthropic request parameters")
    if any(not isinstance(m.get("content"), str) or "tool_calls" in m or m.get("role") == "tool" for m in body["messages"]):
        raise HTTPException(400, "Anthropic adapter supports text chat only in this release")
    if "temperature" in payload and "top_p" in payload:
        raise HTTPException(400, "Choose temperature or top_p for Anthropic")
    system = []
    messages = []
    for message in body["messages"]:
        if message["role"] in {"system", "developer"}:
            system.append(message["content"])
        elif message["role"] in {"user", "assistant"}:
            messages.append({"role": message["role"], "content": message["content"]})
        else:
            raise HTTPException(400, "Unsupported Anthropic message role")
    body = {"model": route["upstream_model"], "messages": messages,
            "max_tokens": payload["max_tokens"], "stream": payload.get("stream", False)}
    if system:
        body["system"] = "\n\n".join(system)
    for field in ["temperature", "top_p", "stop"]:
        if field in payload:
            value = payload[field]
            body["stop_sequences" if field == "stop" else field] = [value] if field == "stop" and isinstance(value, str) else value
    return provider.base_url + "/messages", {"x-api-key": decrypt(provider.encrypted_key),
            "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, body


def response_headers(response, request_id):
    """Forward opaque correlation IDs, never arbitrary upstream headers or error text."""
    headers = {"X-Reservation-ID": request_id, "Cache-Control": "no-store"}
    for name in ("x-request-id", "request-id", "apim-request-id"):
        value = response.headers.get(name, "")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
            headers["X-Provider-Request-ID"] = value
            break
    return headers


def translate_response(provider, data, model):
    if data.get("error"):
        raise ValueError("Provider returned error object")
    if provider.kind in {"openai", "azure_openai"}:
        return data
    blocks = data.get("content", [])
    if not isinstance(blocks, list) or any(not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str) for item in blocks):
        raise ValueError("Unsupported Anthropic response content")
    if data.get("stop_reason") not in {None, "end_turn", "stop_sequence", "max_tokens"}:
        raise ValueError("Unsupported Anthropic stop reason")
    content = "".join(item["text"] for item in blocks)
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    return {"id": data.get("id", ""), "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "length" if data.get("stop_reason") == "max_tokens" else "stop"}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                      "total_tokens": (input_tokens + output_tokens if isinstance(input_tokens, int) and isinstance(output_tokens, int) else None)}}


class StreamTranslator:
    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.usage = None
        self.input_tokens = None
        self.complete = False
        self.message_id = ""

    def event(self, event):
        if not isinstance(event, dict):
            raise ValueError("Invalid SSE event")
        if event.get("error"):
            raise ValueError("Provider stream failed")
        if self.provider.kind in {"openai", "azure_openai"}:
            if isinstance(event, dict) and event.get("usage"):
                self.usage = event["usage"]
            return event
        if not isinstance(event, dict):
            raise ValueError("Invalid SSE event")
        kind = event.get("type")
        delta = {}
        finish = None
        if kind == "message_start":
            message = event.get("message", {})
            self.message_id = message.get("id", "")
            self.input_tokens = message.get("usage", {}).get("input_tokens")
            delta = {"role": "assistant", "content": ""}
        elif kind == "content_block_delta":
            content_delta = event.get("delta", {})
            if content_delta.get("type") not in {None, "text_delta"} or not isinstance(content_delta.get("text"), str):
                raise ValueError("Unsupported Anthropic stream content")
            delta = {"content": content_delta["text"]}
        elif kind == "message_delta":
            out = event.get("usage", {}).get("output_tokens")
            self.usage = {"prompt_tokens": self.input_tokens, "completion_tokens": out,
                          "total_tokens": (self.input_tokens + out if isinstance(self.input_tokens, int) and isinstance(out, int) else None)}
            finish = "length" if event.get("delta", {}).get("stop_reason") == "max_tokens" else "stop"
        elif kind == "message_stop":
            self.complete = True
            return None
        elif kind == "error":
            raise ValueError("Provider stream failed")
        else:
            return None
        result = {"id": self.message_id, "object": "chat.completion.chunk", "model": self.model,
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if self.usage:
            result["usage"] = self.usage
        return result

    async def iterate(self, response):
        # SSE parser supports multi-line data and bounds each event.
        lines = []
        size = 0
        async for line in self.bounded_lines(response):
            if not line:
                if lines:
                    data = "\n".join(lines)
                    lines, size = [], 0
                    if data == "[DONE]":
                        self.complete = True
                        yield b"data: [DONE]\n\n"
                        return
                    event = self.event(json.loads(data))
                    if event is not None:
                        yield ("data: " + json.dumps(event) + "\n\n").encode()
                continue
            if line.startswith("data:"):
                value = line[5:].lstrip(" ")
                size += len(value)
                if size > 1_048_576:
                    raise ValueError("Upstream event too large")
                lines.append(value)
        if lines:
            data = "\n".join(lines)
            if data == "[DONE]":
                self.complete = True
            else:
                event = self.event(json.loads(data))
                if event is not None:
                    yield ("data: " + json.dumps(event) + "\n\n").encode()
        if not self.complete:
            raise ValueError("Provider stream ended without completion marker")
        yield b"data: [DONE]\n\n"

    @staticmethod
    async def bounded_lines(response):
        # httpx.aiter_lines buffers until newline with no length limit. Bound the
        # buffer before decoding, including non-data/comment lines.
        pending = bytearray()
        async for chunk in response.aiter_bytes():
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                if len(line) > 1_048_576:
                    raise ValueError("Upstream line too large")
                yield line.rstrip(b"\r").decode("utf-8")
            if len(pending) > 1_048_576:
                raise ValueError("Upstream line too large")
        if pending:
            yield pending.rstrip(b"\r").decode("utf-8")
