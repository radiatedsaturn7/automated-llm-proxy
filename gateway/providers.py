import asyncio
import ipaddress
import json
import math
import socket
import time
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
    if provider.kind == "bedrock_converse":
        from .bedrock import request
        return request(provider, route, payload, endpoint)
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
    allowed = {'model', 'messages', 'max_tokens', 'temperature', 'top_p', 'stop', 'stream',
               'tools', 'tool_choice', 'parallel_tool_calls'}
    if set(payload) - allowed:
        raise HTTPException(400, "Unsupported Anthropic request parameters")
    if "temperature" in payload and "top_p" in payload:
        raise HTTPException(400, "Choose temperature or top_p for Anthropic")
    try:
        tool_options = anthropic_tools(payload)
        system, messages = anthropic_messages(body["messages"])
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise HTTPException(400, "Invalid or unsupported Anthropic tool/message parameters") from exc
    body = {"model": route["upstream_model"], "messages": messages,
            "max_tokens": payload["max_tokens"], "stream": payload.get("stream", False)}
    body.update(tool_options)
    if system:
        body["system"] = "\n\n".join(system)
    for field in ["temperature", "top_p", "stop"]:
        if field in payload:
            value = payload[field]
            body["stop_sequences" if field == "stop" else field] = [value] if field == "stop" and isinstance(value, str) else value
    return provider.base_url + "/messages", {"x-api-key": decrypt(provider.encrypted_key),
            "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, body


def json_object(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = item
        return result
    def invalid(value):
        raise ValueError('Non-finite JSON number')
    if not isinstance(value, str) or len(value) > 1_048_576:
        raise ValueError('Invalid tool arguments')
    try:
        obj = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
        json.dumps(obj, allow_nan=False)  # Also reject finite-literal overflow, e.g. 1e309.
    except RecursionError as exc:
        raise ValueError('Tool JSON nesting too deep') from exc
    if not isinstance(obj, dict):
        raise ValueError('Tool arguments must be an object')
    return obj


def tool_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid tool name')
    return value


def tool_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
        raise ValueError('Invalid tool ID')
    return value


def anthropic_tools(payload):
    result = {}
    if 'tools' not in payload:
        if 'tool_choice' in payload or 'parallel_tool_calls' in payload:
            raise ValueError('Tool controls require tools')
        return result
    tools = payload['tools']
    if not isinstance(tools, list) or not 1 <= len(tools) <= 128:
        raise ValueError('Between one and 128 tools required')
    names, translated = set(), []
    for item in tools:
        if not isinstance(item, dict) or set(item) != {'type', 'function'} or item['type'] != 'function':
            raise ValueError('Only function tools supported')
        fn = item['function']
        if not isinstance(fn, dict) or set(fn) - {'name', 'description', 'parameters'}:
            raise ValueError('Unsupported function parameters; strict schemas not supported')
        name = tool_name(fn.get('name'))
        if name in names:
            raise ValueError('Duplicate function name')
        names.add(name)
        schema = fn.get('parameters', {'type': 'object', 'properties': {}})
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ValueError('Function parameters must be an object schema')
        # Reject non-JSON and non-finite values rather than emitting invalid JSON.
        json.dumps(schema, allow_nan=False)
        tool = {'name': name, 'input_schema': schema}
        if 'description' in fn:
            if not isinstance(fn['description'], str):
                raise ValueError('Invalid function description')
            tool['description'] = fn['description']
        translated.append(tool)
    choice = payload.get('tool_choice', 'auto')
    if isinstance(choice, str) and choice in {'auto', 'required', 'none'}:
        choice = {'type': {'auto': 'auto', 'required': 'any', 'none': 'none'}[choice]}
    elif (isinstance(choice, dict) and set(choice) == {'type', 'function'} and choice['type'] == 'function'
          and isinstance(choice['function'], dict) and set(choice['function']) == {'name'}
          and choice['function']['name'] in names):
        choice = {'type': 'tool', 'name': choice['function']['name']}
    else:
        raise ValueError('Invalid tool choice')
    if 'parallel_tool_calls' in payload:
        if type(payload['parallel_tool_calls']) is not bool or choice['type'] == 'none':
            raise ValueError('Unsupported parallel tool setting')
        choice['disable_parallel_tool_use'] = not payload['parallel_tool_calls']
    return {'tools': translated, 'tool_choice': choice}


def anthropic_messages(source):
    system, messages, pending, seen = [], [], set(), set()
    for message in source:
        if not isinstance(message, dict) or set(message) - {'role', 'content', 'tool_calls', 'tool_call_id'}:
            raise ValueError('Unsupported Anthropic message fields')
        role, content = message.get('role'), message.get('content')
        if role == 'tool':
            ident = tool_id(message.get('tool_call_id'))
            if ident not in pending or not isinstance(content, str) or 'tool_calls' in message:
                raise ValueError('Unmatched tool result')
            pending.remove(ident)
            block = {'type': 'tool_result', 'tool_use_id': ident, 'content': content}
            if messages[-1]['role'] == 'user':
                messages[-1]['content'].append(block)
            else:
                messages.append({'role': 'user', 'content': [block]})
            continue
        if pending or 'tool_call_id' in message:
            raise ValueError('Tool calls require all matching results before next turn')
        if role in {'system', 'developer'}:
            if messages or not isinstance(content, str) or 'tool_calls' in message:
                raise ValueError('System messages must precede conversation')
            system.append(content)
        elif role in {'user', 'assistant'}:
            calls = message.get('tool_calls')
            if calls is None:
                if not isinstance(content, str):
                    raise ValueError('Text content required')
                messages.append({'role': role, 'content': content})
                continue
            if role != 'assistant' or not isinstance(calls, list) or not 1 <= len(calls) <= 128:
                raise ValueError('Invalid assistant tool calls')
            if content is not None and not isinstance(content, str):
                raise ValueError('Text content required')
            blocks = [{'type': 'text', 'text': content}] if content else []
            for call in calls:
                if not isinstance(call, dict) or set(call) != {'id', 'type', 'function'} or call['type'] != 'function':
                    raise ValueError('Invalid tool call')
                ident = tool_id(call['id'])
                if ident in seen:
                    raise ValueError('Duplicate tool ID')
                fn = call['function']
                if not isinstance(fn, dict) or set(fn) != {'name', 'arguments'}:
                    raise ValueError('Invalid tool function')
                blocks.append({'type': 'tool_use', 'id': ident, 'name': tool_name(fn['name']), 'input': json_object(fn['arguments'])})
                pending.add(ident)
                seen.add(ident)
            messages.append({'role': 'assistant', 'content': blocks})
        else:
            raise ValueError('Unsupported Anthropic message role')
    if pending or not messages:
        raise ValueError('Conversation must contain messages and resolve every tool call')
    return system, messages


def anthropic_content(blocks):
    if not isinstance(blocks, list):
        raise ValueError('Invalid Anthropic content')
    text, calls, seen = [], [], set()
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError('Invalid Anthropic block')
        if block.get('type') == 'text' and isinstance(block.get('text'), str) and not set(block) - {'type', 'text'}:
            text.append(block['text'])
        elif block.get('type') == 'tool_use' and set(block) == {'type', 'id', 'name', 'input'}:
            ident = tool_id(block['id'])
            if ident in seen or len(calls) >= 128 or not isinstance(block['input'], dict):
                raise ValueError('Invalid tool response')
            seen.add(ident)
            arguments = json.dumps(block['input'], allow_nan=False, separators=(',', ':'))
            if len(arguments) > 1_048_576:
                raise ValueError('Tool arguments too large')
            calls.append({'id': ident, 'type': 'function', 'function': {'name': tool_name(block['name']), 'arguments': arguments}})
        else:
            raise ValueError('Unsupported Anthropic response block')
    message = {'role': 'assistant', 'content': ''.join(text) if text or not calls else None}
    if calls:
        message['tool_calls'] = calls
    return message


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


def anthropic_finish(reason):
    if reason in {'max_tokens', 'model_context_window_exceeded'}:
        return 'length'
    if reason == 'refusal':
        return 'content_filter'
    return 'tool_calls' if reason == 'tool_use' else 'stop'


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
    if provider.kind == "bedrock_converse":
        from .bedrock import response
        return response(data, model)
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
    message = anthropic_content(data.get("content", []))
    reason = data.get("stop_reason")
    if reason not in {"end_turn", "stop_sequence", "max_tokens", "tool_use", "refusal", "model_context_window_exceeded"}:
        raise ValueError("Unsupported Anthropic stop reason")
    if (reason == 'tool_use') != bool(message.get('tool_calls')) and reason not in {'max_tokens', 'model_context_window_exceeded', 'refusal'}:
        raise ValueError('Tool content and stop reason mismatch')
    if reason == 'refusal':
        # Never expose tool calls as executable after a refusal. Preserve only
        # the refusal text, without forwarding provider stop_details metadata.
        message = {'role': 'assistant', 'content': None, 'refusal': message.get('content') or ''}
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    return {"id": data.get("id", ""), "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": anthropic_finish(reason)}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                      "total_tokens": (input_tokens + output_tokens if isinstance(input_tokens, int) and isinstance(output_tokens, int) else None)}}


class StreamTranslator:
    def __init__(self, provider, model, *, max_bytes=16_777_216, max_events=100_000,
                 duration_seconds=300, deadline=None):
        self.provider, self.model = provider, model
        self.max_bytes, self.max_events = max_bytes, max_events
        self.duration_seconds, self.deadline = duration_seconds, deadline
        self.received_bytes = 0
        self.received_events = 0
        self.usage = None
        self.input_tokens = None
        self.complete = False
        self.message_id = ""
        self.anthropic_started = False
        self.anthropic_finished = False
        self.anthropic_blocks = {}
        self.anthropic_ids = set()
        self.anthropic_argument_size = 0

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
        if kind == 'ping':
            return None
        if self.complete or (self.anthropic_finished and kind != 'message_stop'):
            raise ValueError('Anthropic content after finish')
        delta, finish = {}, None
        if kind == "message_start":
            if self.anthropic_started:
                raise ValueError('Duplicate message start')
            message = event.get('message', {})
            if not isinstance(message, dict) or message.get('content', []) != []:
                raise ValueError('Invalid message start')
            self.anthropic_started = True
            self.message_id = message.get('id', '')
            self.input_tokens = message.get('usage', {}).get('input_tokens')
            delta = {'role': 'assistant', 'content': ''}
        elif not self.anthropic_started:
            raise ValueError('Anthropic stream missing message start')
        elif kind == 'content_block_start':
            index = event.get('index')
            block = event.get('content_block')
            if type(index) is not int or index != len(self.anthropic_blocks) or index >= 1024:
                raise ValueError('Invalid content block index')
            if any(not b['closed'] for b in self.anthropic_blocks.values()):
                raise ValueError('Overlapping content blocks')
            message = anthropic_content([block])
            state = {'closed': False, 'kind': block['type']}
            if block['type'] == 'text':
                delta = {'content': message['content']}
            else:
                if block['input'] != {} or block['id'] in self.anthropic_ids or len(self.anthropic_ids) >= 128:
                    raise ValueError('Invalid streaming tool start')
                state.update(tool_index=len(self.anthropic_ids), arguments='')
                self.anthropic_ids.add(block['id'])
                call = message['tool_calls'][0]
                call['function']['arguments'] = ''
                delta = {'tool_calls': [{'index': state['tool_index'], **call}]}
            self.anthropic_blocks[index] = state
        elif kind in {'content_block_delta', 'content_block_stop'}:
            index = event.get('index')
            if type(index) is not int or index not in self.anthropic_blocks or self.anthropic_blocks[index]['closed']:
                raise ValueError('Unknown or closed content block')
            state = self.anthropic_blocks[index]
            if kind == 'content_block_stop':
                state['closed'] = True
                if state['kind'] == 'tool_use':
                    if state['arguments']:
                        json_object(state['arguments'])
                    else:
                        delta = {'tool_calls': [{'index': state['tool_index'], 'function': {'arguments': '{}'}}]}
                if not delta:
                    return None
            else:
                part = event.get('delta', {})
                if not isinstance(part, dict):
                    raise ValueError('Invalid content delta')
                if state['kind'] == 'text' and set(part) == {'type', 'text'} and part['type'] == 'text_delta' and isinstance(part['text'], str):
                    delta = {'content': part['text']}
                elif state['kind'] == 'tool_use' and set(part) == {'type', 'partial_json'} and part['type'] == 'input_json_delta' and isinstance(part['partial_json'], str):
                    self.anthropic_argument_size += len(part['partial_json'])
                    if self.anthropic_argument_size > 1_048_576:
                        raise ValueError('Tool arguments too large')
                    state['arguments'] += part['partial_json']
                    delta = {'tool_calls': [{'index': state['tool_index'], 'function': {'arguments': part['partial_json']}}]}
                else:
                    raise ValueError('Unsupported Anthropic stream content')
        elif kind == 'message_delta':
            if any(not b['closed'] for b in self.anthropic_blocks.values()):
                raise ValueError('Unclosed content block')
            reason = event.get('delta', {}).get('stop_reason')
            if reason not in {'end_turn', 'stop_sequence', 'max_tokens', 'tool_use', 'refusal', 'model_context_window_exceeded'}:
                raise ValueError('Unsupported Anthropic stop reason')
            if (reason == 'tool_use') != bool(self.anthropic_ids) and reason not in {'max_tokens', 'model_context_window_exceeded', 'refusal'}:
                raise ValueError('Tool content and stop reason mismatch')
            out = event.get('usage', {}).get('output_tokens')
            self.usage = {'prompt_tokens': self.input_tokens, 'completion_tokens': out,
                          'total_tokens': (self.input_tokens + out if isinstance(self.input_tokens, int) and isinstance(out, int) else None)}
            finish = anthropic_finish(reason)
            self.anthropic_finished = True
        elif kind == 'message_stop':
            if not self.anthropic_finished:
                raise ValueError('Anthropic stream missing finish reason')
            self.complete = True
            return None
        else:
            raise ValueError('Unsupported Anthropic stream event')
        result = {'id': self.message_id, 'object': 'chat.completion.chunk', 'model': self.model,
                  'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        if self.usage:
            result['usage'] = self.usage
        return result

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.complete = False
            raise ValueError("Upstream stream duration exceeded")
        return remaining

    def count_event(self):
        self.remaining()
        self.received_events += 1
        if self.received_events > self.max_events:
            self.complete = False
            raise ValueError("Upstream stream event limit exceeded")

    async def iterate(self, response):
        if self.deadline is None:
            self.deadline = time.monotonic() + self.duration_seconds
        # SSE parser supports multi-line data and bounds each event.
        lines = []
        size = 0
        frame_started = False
        async for line in self.bounded_lines(response):
            self.remaining()
            if not line:
                if frame_started:
                    self.count_event()  # Includes comment-only and unknown-field frames.
                    frame_started = False
                if lines:
                    data = "\n".join(lines)
                    lines, size = [], 0
                    if data == "[DONE]":
                        if self.provider.kind in {"gemini", "anthropic"} and not self.complete:
                            raise ValueError("Native provider stream missing completion")
                        self.complete = True
                        self.remaining()
                        yield b"data: [DONE]\n\n"
                        return
                    event = self.event(json_object(data))
                    if event is not None:
                        encoded = ("data: " + json.dumps(event) + "\n\n").encode()
                        self.remaining()
                        yield encoded
                continue
            frame_started = True
            if line.startswith("data:"):
                value = line[5:].lstrip(" ")
                size += len(value)
                if size > 1_048_576:
                    raise ValueError("Upstream event too large")
                lines.append(value)
        if frame_started:
            self.count_event()
        self.remaining()
        if lines:
            data = "\n".join(lines)
            if data == "[DONE]":
                if self.provider.kind in {"gemini", "anthropic"} and not self.complete:
                    raise ValueError("Native provider stream missing completion")
                self.complete = True
            else:
                event = self.event(json_object(data))
                if event is not None:
                    encoded = ("data: " + json.dumps(event) + "\n\n").encode()
                    self.remaining()
                    yield encoded
        if not self.complete:
            raise ValueError("Provider stream ended without completion marker")
        self.remaining()
        yield b"data: [DONE]\n\n"

    async def bounded_lines(self, response):
        # httpx.aiter_lines buffers until newline with no length limit. Bound the
        # buffer before decoding, including non-data/comment lines.
        pending = bytearray()
        chunks = response.aiter_bytes().__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(anext(chunks), timeout=self.remaining())
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                self.complete = False
                raise ValueError("Upstream stream duration exceeded") from exc
            self.remaining()
            self.received_bytes += len(chunk)
            if self.received_bytes > self.max_bytes:
                self.complete = False
                raise ValueError("Upstream stream byte limit exceeded")
            pending.extend(chunk)
            offset = 0
            lines = 0
            while True:
                end = pending.find(b"\n", offset)
                if end < 0:
                    break
                if end - offset > 1_048_576:
                    raise ValueError("Upstream line too large")
                line = pending[offset:end]
                offset = end + 1
                lines += 1
                # A transport can deliver thousands of blank/comment lines in
                # one chunk. Yield fairly even when the parser emits no events.
                if lines % 256 == 0:
                    await asyncio.sleep(0)
                    self.remaining()
                yield line.rstrip(b"\r").decode("utf-8")
            if offset:
                del pending[:offset]
            if len(pending) > 1_048_576:
                raise ValueError("Upstream line too large")
        if pending:
            yield pending.rstrip(b"\r").decode("utf-8")
