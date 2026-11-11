"""Stateless, text-only Responses compatibility over the normal chat pipeline."""
import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .inference import infer

router = APIRouter()


def chat_payload(payload):
    allowed = {'model', 'input', 'instructions', 'max_output_tokens', 'temperature', 'top_p', 'store', 'stream'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise HTTPException(400, 'Unsupported Responses parameters; only stateless text requests are supported')
    for flag in ('store', 'stream'):
        if flag in payload and payload[flag] is not False:
            raise HTTPException(400, f'Responses {flag} must be false')
    messages = []
    if 'instructions' in payload:
        if not isinstance(payload['instructions'], str):
            raise HTTPException(400, 'Responses instructions must be text')
        messages.append({'role': 'system', 'content': payload['instructions']})
    content = payload.get('input')
    if isinstance(content, str):
        messages.append({'role': 'user', 'content': content})
    elif isinstance(content, list) and 1 <= len(content) <= 999:
        for item in content:
            if (not isinstance(item, dict) or set(item) - {'role', 'content', 'type'}
                    or item.get('type', 'message') != 'message'
                    or not isinstance(item.get('role'), str) or item.get('role') not in {'user', 'assistant', 'system', 'developer'}):
                raise HTTPException(400, 'Responses input supports text messages only')
            text = item.get('content')
            if isinstance(text, list) and 1 <= len(text) <= 100:
                parts = []
                for part in text:
                    expected = {'input_text', 'output_text'} if item['role'] == 'assistant' else {'input_text'}
                    if (not isinstance(part, dict) or set(part) != {'type', 'text'}
                            or not isinstance(part.get('type'), str) or part.get('type') not in expected or not isinstance(part.get('text'), str)):
                        raise HTTPException(400, 'Responses content supports text parts only')
                    parts.append(part['text'])
                text = ''.join(parts)
            if not isinstance(text, str):
                raise HTTPException(400, 'Responses message content must be text')
            messages.append({'role': item['role'], 'content': text})
    else:
        raise HTTPException(400, 'Responses input must be text or a nonempty message list')
    result = {'model': payload.get('model'), 'messages': messages, 'stream': False}
    for source, target in [('max_output_tokens', 'max_tokens'), ('temperature', 'temperature'), ('top_p', 'top_p')]:
        if source in payload:
            result[target] = payload[source]
    return result


def response_envelope(data, model):
    choices = data.get('choices') if isinstance(data, dict) else None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError('Expected one text completion')
    message = choices[0].get('message')
    finish = choices[0].get('finish_reason')
    if not isinstance(message, dict) or message.get('tool_calls') or message.get('function_call') or finish not in {'stop', 'length', 'content_filter'}:
        raise ValueError('Unsupported chat response')
    content, refusal = message.get('content'), message.get('refusal')
    parts = []
    if content is not None:
        if not isinstance(content, str):
            raise ValueError('Expected text output')
        parts.append({'type': 'output_text', 'text': content, 'annotations': []})
    if refusal is not None:
        if not isinstance(refusal, str):
            raise ValueError('Expected text refusal')
        parts.append({'type': 'refusal', 'refusal': refusal})
    if not parts and finish != 'content_filter':
        raise ValueError('Missing text output')
    status = 'completed' if finish == 'stop' else 'incomplete'
    usage = None
    reported = data.get('usage')
    if isinstance(reported, dict):
        inp, out = reported.get('prompt_tokens'), reported.get('completion_tokens')
        if all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (inp, out)):
            usage = {'input_tokens': inp, 'output_tokens': out, 'total_tokens': inp + out}
    return {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'created_at': int(time.time()),
            'status': status, 'error': None, 'incomplete_details': None if status == 'completed' else {
                'reason': 'max_output_tokens' if finish == 'length' else 'content_filter'},
            'model': model, 'store': False, 'background': False, 'instructions': None,
            'previous_response_id': None, 'tools': [], 'tool_choice': 'none', 'parallel_tool_calls': False,
            'metadata': {}, 'usage': usage,
            'output': [{'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                        'status': status, 'content': parts}]}


@router.post('/v1/responses')
async def create_response(request: Request):
    try:
        payload = chat_payload(await request.json())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid JSON') from None
    # Keep the same app/state/headers and cancellation context. No loopback HTTP,
    # persistent conversation, duplicate reservation or downstream credential.
    body = json.dumps(payload).encode()
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    result = await infer(Request(request.scope, receive=receive), 'chat/completions')
    if result.status_code != 200:
        return result
    try:
        envelope = response_envelope(json.loads(result.body), payload['model'])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(502, 'Provider response cannot be represented as stateless text') from None
    # JSONResponse recomputes body length; preserve only deliberate safe headers.
    headers = {key: value for key, value in result.headers.items()
               if key.lower() in {'x-reservation-id', 'x-provider-request-id', 'cache-control'}}
    return JSONResponse(envelope, headers=headers)
