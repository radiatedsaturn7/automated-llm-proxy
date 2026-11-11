import asyncio
import ipaddress
import json
import math
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
    if provider.kind == "gemini":
        return gemini_request(provider, route, payload, endpoint)
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


def gemini_request(provider, route, payload, endpoint):
    model = route['upstream_model'].removeprefix('models/')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', model) or '..' in model:
        raise HTTPException(400, 'Invalid Gemini model ID')
    headers = {'Content-Type': 'application/json', 'x-goog-api-key': decrypt(provider.encrypted_key)}
    url = provider.base_url.rstrip('/') + '/models/' + model
    if endpoint == 'embeddings':
        if payload.get('encoding_format', 'float') != 'float':
            raise HTTPException(400, 'Gemini embeddings support float encoding only')
        dimension = payload.get('dimensions')
        if dimension is not None and (isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 3072):
            raise HTTPException(400, 'Invalid Gemini embedding dimensions')
        inputs = payload['input'] if isinstance(payload['input'], list) else [payload['input']]
        if len(inputs) > 100:
            raise HTTPException(400, 'Gemini embedding batches are limited to 100 texts')
        requests = [{'model': 'models/' + model, 'content': {'parts': [{'text': value}]}} for value in inputs]
        if dimension is not None:
            for request in requests:
                request['outputDimensionality'] = dimension
        return url + ':batchEmbedContents', headers, {'requests': requests}
    allowed = {'model', 'messages', 'max_tokens', 'temperature', 'top_p', 'stop', 'stream', 'stream_options'}
    if endpoint != 'chat/completions' or set(payload) - allowed:
        raise HTTPException(400, 'Unsupported Gemini request parameters')
    contents, system = [], []
    for message in payload['messages']:
        if set(message) - {'role', 'content'} or not isinstance(message.get('content'), str):
            raise HTTPException(400, 'Gemini adapter supports unnamed text messages only')
        role, content = message['role'], message['content']
        if role in {'system', 'developer'}:
            system.append({'text': content})
        elif role in {'user', 'assistant'}:
            contents.append({'role': 'model' if role == 'assistant' else 'user', 'parts': [{'text': content}]})
        else:
            raise HTTPException(400, 'Unsupported Gemini message role')
    if not contents:
        raise HTTPException(400, 'Gemini requires a user or assistant message')
    generation = {'maxOutputTokens': payload.get('max_tokens', 512), 'candidateCount': 1}
    for source, target in [('temperature', 'temperature'), ('top_p', 'topP')]:
        if source in payload:
            value = payload[source]
            if not 0 <= value <= (2 if source == 'temperature' else 1):
                raise HTTPException(400, 'Invalid Gemini sampling parameter')
            generation[target] = value
    if 'stop' in payload:
        stops = [payload['stop']] if isinstance(payload['stop'], str) else payload['stop']
        if not isinstance(stops, list) or not 1 <= len(stops) <= 5 or any(not isinstance(x, str) or not x for x in stops):
            raise HTTPException(400, 'Invalid Gemini stop sequences')
        generation['stopSequences'] = stops
    body = {'contents': contents, 'generationConfig': generation}
    if system:
        body['systemInstruction'] = {'parts': system}
    action = ':streamGenerateContent?alt=sse' if payload.get('stream') else ':generateContent'
    return url + action, headers, body


def gemini_usage(data):
    metadata = data.get('usageMetadata')
    if not isinstance(metadata, dict):
        return None
    # Charge thinking tokens as output; never expose thought text to clients.
    inp = metadata.get('promptTokenCount')
    out, thoughts = metadata.get('candidatesTokenCount'), metadata.get('thoughtsTokenCount', 0)
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (inp, out, thoughts)):
        return None
    return {'prompt_tokens': inp, 'completion_tokens': out + thoughts, 'total_tokens': inp + out + thoughts}


def gemini_candidate(data, *, terminal):
    candidates = data.get('candidates', [])
    feedback = data.get('promptFeedback', {})
    if not candidates and isinstance(feedback, dict) and feedback.get('blockReason'):
        return '', 'content_filter'
    if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
        raise ValueError('Invalid Gemini candidate response')
    candidate = candidates[0]
    if candidate.get('index', 0) != 0:
        raise ValueError('Unexpected Gemini candidate index')
    parts = candidate.get('content', {}).get('parts', [])
    if not isinstance(parts, list):
        raise ValueError('Invalid Gemini response parts')
    text_parts = []
    for part in parts:
        if not isinstance(part, dict) or 'text' not in part or not isinstance(part['text'], str) or set(part) - {'text', 'thought', 'thoughtSignature'}:
            raise ValueError('Unsupported Gemini response part')
        if not part.get('thought', False):
            text_parts.append(part['text'])
    reason = candidate.get('finishReason')
    reasons = {'STOP': 'stop', 'MAX_TOKENS': 'length', 'SAFETY': 'content_filter',
               'RECITATION': 'content_filter', 'BLOCKLIST': 'content_filter',
               'PROHIBITED_CONTENT': 'content_filter', 'SPII': 'content_filter'}
    if reason is not None and reason not in reasons:
        raise ValueError('Unsupported Gemini finish reason')
    if terminal and reason is None:
        raise ValueError('Incomplete Gemini response')
    finish = reasons.get(reason)
    return ('' if finish == 'content_filter' else ''.join(text_parts)), finish


def gemini_response(data, model):
    if 'embeddings' in data:
        embeddings = data['embeddings']
        if not isinstance(embeddings, list) or not 1 <= len(embeddings) <= 100:
            raise ValueError('Invalid Gemini embeddings')
        result = []
        for index, item in enumerate(embeddings):
            vector = item.get('values') if isinstance(item, dict) else None
            if not isinstance(vector, list) or not 1 <= len(vector) <= 3072 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
                raise ValueError('Invalid Gemini embedding vector')
            result.append({'object': 'embedding', 'index': index, 'embedding': vector})
        # Batch native responses may omit usage; let normal conservative billing apply.
        translated = {'object': 'list', 'model': model, 'data': result}
        metadata = data.get('usageMetadata')
        count = metadata.get('promptTokenCount') if isinstance(metadata, dict) else None
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            translated['usage'] = {'prompt_tokens': count, 'completion_tokens': 0, 'total_tokens': count}
        return translated
    content, finish = gemini_candidate(data, terminal=True)
    return {'id': data.get('responseId', ''), 'object': 'chat.completion', 'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': finish}],
            'usage': gemini_usage(data)}


def response_headers(response, request_id):
    """Forward opaque correlation IDs, never arbitrary upstream headers or error text."""
    headers = {"X-Reservation-ID": request_id, "Cache-Control": "no-store"}
    for name in ("x-request-id", "request-id", "apim-request-id"):
        value = response.headers.get(name, "")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
            headers["X-Provider-Request-ID"] = value
            break
    return headers


def translate_response(provider, data, model, endpoint=None, payload=None):
    if data.get("error"):
        raise ValueError("Provider returned error object")
    if provider.kind in {"openai", "azure_openai"}:
        return data
    if provider.kind == "gemini":
        if endpoint is not None and (endpoint == 'embeddings') != ('embeddings' in data):
            raise ValueError('Gemini response does not match requested operation')
        translated = gemini_response(data, model)
        if endpoint == 'embeddings' and payload is not None:
            expected = len(payload['input']) if isinstance(payload['input'], list) else 1
            if len(translated['data']) != expected:
                raise ValueError('Gemini embedding count mismatch')
            dimension = payload.get('dimensions')
            sizes = {len(item['embedding']) for item in translated['data']}
            if len(sizes) != 1 or (dimension is not None and sizes != {dimension}):
                raise ValueError('Gemini embedding dimension mismatch')
        return translated
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
        if self.provider.kind == "gemini":
            usage = gemini_usage(event)
            if usage is not None:
                self.usage = usage
            if not event.get('candidates') and not event.get('promptFeedback') and usage is not None:
                return {'id': self.message_id, 'object': 'chat.completion.chunk', 'model': self.model,
                        'choices': [], 'usage': usage}
            content, finish = gemini_candidate(event, terminal=False)
            if self.complete:
                raise ValueError('Gemini content after completion')
            self.message_id = event.get('responseId', self.message_id)
            self.complete = finish is not None
            if self.complete:
                # An earlier chunk's cumulative count is not final usage.
                self.usage = usage
            result = {'id': self.message_id, 'object': 'chat.completion.chunk', 'model': self.model,
                      'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': finish}]}
            if self.usage is not None:
                result['usage'] = self.usage
            return result
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
                        if self.provider.kind == "gemini" and not self.complete:
                            raise ValueError("Gemini stream missing finish reason")
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
                if self.provider.kind == "gemini" and not self.complete:
                    raise ValueError("Gemini stream missing finish reason")
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
