"""Amazon Bedrock Converse: bounded text-only REST with documented bearer keys."""
import math
import re
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import HTTPException
from .security import decrypt


def request(provider, route, payload, endpoint):
    allowed = {'model', 'messages', 'max_tokens', 'temperature', 'top_p', 'stop', 'stream'}
    if endpoint != 'chat/completions' or payload.get('stream') or set(payload) - allowed:
        raise HTTPException(400, 'Bedrock Converse supports nonstreaming text chat only')
    parsed = urlsplit(provider.base_url)
    if (parsed.scheme != 'https' or not re.fullmatch(r'bedrock-runtime\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?', parsed.netloc)
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
        raise HTTPException(400, 'Bedrock Converse requires a regional bedrock-runtime HTTPS origin')
    model = route['upstream_model']
    # Initial scope: model/inference-profile IDs, not ARNs or prompt resources.
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', model) or '..' in model or model.startswith('arn:'):
        raise HTTPException(400, 'Bedrock model or inference profile ID required; ARNs are unsupported')
    messages, system = [], []
    source = payload.get('messages')
    if not isinstance(source, list) or not 1 <= len(source) <= 1000:
        raise HTTPException(400, 'Bedrock text messages required')
    for message in source:
        if (not isinstance(message, dict) or set(message) != {'role','content'}
                or not isinstance(message['content'], str) or not message['content']):
            raise HTTPException(400, 'Bedrock supports plain text messages only')
        role = message['role']
        if role in {'system','developer'} and not messages:
            system.append({'text':message['content']})
        elif role in {'user','assistant'}:
            messages.append({'role':role,'content':[{'text':message['content']}]})
        else:
            raise HTTPException(400, 'Bedrock system messages must precede user/assistant conversation')
    if not messages or messages[0]['role'] != 'user':
        raise HTTPException(400, 'Bedrock conversation must start with a user message')
    count = payload.get('max_tokens')
    if type(count) is not int or count < 1:
        raise HTTPException(400, 'Bedrock max_tokens must be a positive integer')
    config = {'maxTokens':count}
    for incoming, outgoing in [('temperature','temperature'),('top_p','topP')]:
        if incoming in payload:
            value = payload[incoming]
            if type(value) not in {int,float} or not math.isfinite(value) or not 0 <= value <= 1:
                raise HTTPException(400, 'Bedrock sampling parameters must be within zero and one')
            config[outgoing] = value
    if 'stop' in payload:
        value = payload['stop']
        value = [value] if isinstance(value,str) else value
        if not isinstance(value,list) or not 1 <= len(value) <= 4 or any(not isinstance(v,str) or not 1 <= len(v) <= 1000 for v in value):
            raise HTTPException(400, 'Bedrock requires one to four nonempty stop strings')
        config['stopSequences'] = value
    body = {'messages':messages,'inferenceConfig':config}
    if system:
        body['system'] = system
    return provider.base_url.rstrip('/') + '/model/' + quote(model,safe='') + '/converse', {
        'Content-Type':'application/json', 'Authorization':'Bearer ' + decrypt(provider.encrypted_key)}, body


def response(data, model):
    if not isinstance(data,dict) or data.get('error'):
        raise ValueError('Invalid Bedrock response')
    output, usage, reason = data.get('output'), data.get('usage'), data.get('stopReason')
    reasons = {'end_turn':'stop','stop_sequence':'stop','max_tokens':'length',
               'guardrail_intervened':'content_filter','content_filtered':'content_filter'}
    if reason not in reasons or not isinstance(output,dict) or not isinstance(usage,dict):
        raise ValueError('Invalid Bedrock completion')
    message = output.get('message')
    if not isinstance(message,dict) or message.get('role') != 'assistant':
        raise ValueError('Invalid Bedrock message')
    content = message.get('content')
    if not isinstance(content,list) or not content:
        raise ValueError('Invalid Bedrock content')
    if any(not isinstance(block,dict) or set(block) != {'text'} or not isinstance(block['text'],str) for block in content):
        raise ValueError('Unsupported Bedrock content')
    # No prompt-cache input is requested; refuse unexpected billing categories
    # rather than silently price them as ordinary tokens.
    for field in ('cacheReadInputTokens','cacheWriteInputTokens'):
        if field in usage and (type(usage[field]) is not int or usage[field] != 0):
            raise ValueError('Unsupported Bedrock cache billing')
    counts = [usage.get(name) for name in ('inputTokens','outputTokens','totalTokens')]
    if any(type(n) is not int or not 0 <= n <= 2**53-1 for n in counts) or counts[2] != counts[0]+counts[1]:
        raise ValueError('Invalid Bedrock usage')
    return {'id':'chatcmpl-' + uuid4().hex,'object':'chat.completion','created':int(time.time()),'model':model,
            'choices':[{'index':0,'message':{'role':'assistant','content':''.join(b['text'] for b in content)},
                        'finish_reason':reasons[reason]}],
            'usage':{'prompt_tokens':counts[0],'completion_tokens':counts[1],'total_tokens':counts[2]}}
