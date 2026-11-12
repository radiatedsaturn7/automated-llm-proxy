"""Synthetic contracts only: no AWS credential chain or provider network calls."""
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from gateway import db
from gateway.providers import upstream_request, translate_response
from gateway.security import encrypt
from test_gateway import setup_inference, mock_upstream

CHAT = {'model':'chat','messages':[{'role':'user','content':'hello'}],'max_tokens':64}
ROUTE = {'upstream_model':'us.anthropic.claude-sonnet-4-6'}
BASE = 'https://bedrock-runtime.us-east-1.amazonaws.com'


def provider(base=BASE):
    return SimpleNamespace(kind='bedrock_converse',base_url=base,encrypted_key=encrypt('bedrock-secret'))


def reply():
    return {'output':{'message':{'role':'assistant','content':[{'text':'hello'}]}},
            'stopReason':'end_turn','usage':{'inputTokens':10,'outputTokens':3,'totalTokens':13},
            'metrics':{'latencyMs':1}}


async def native(client, admin_headers):
    headers, key, old = await setup_inference(client, admin_headers)
    async with db.transaction() as session:
        item = await session.get(db.Provider,old['id'])
        item.kind, item.base_url = 'bedrock_converse', BASE
        item.models = {'chat': {**item.models['chat'], 'upstream_model': ROUTE['upstream_model']}}
    return headers, key, old


async def test_native_contract(client):
    url, headers, body = upstream_request(provider(),{'upstream_model':'amazon.nova-lite-v1:0'},CHAT | {
        'messages':[{'role':'system','content':'be nice'},{'role':'developer','content':'concise'},*CHAT['messages']],
        'temperature':.2,'top_p':.8,'stop':'END'},'chat/completions')
    assert url == BASE+'/model/amazon.nova-lite-v1%3A0/converse'
    assert headers == {'Content-Type':'application/json','Authorization':'Bearer bedrock-secret'}
    assert body == {'system':[{'text':'be nice'},{'text':'concise'}],
                    'messages':[{'role':'user','content':[{'text':'hello'}]}],
                    'inferenceConfig':{'maxTokens':64,'temperature':.2,'topP':.8,'stopSequences':['END']}}
    assert 'secret' not in url and 'model' not in body


@pytest.mark.parametrize('bad', ['../x','foo/bar','arn:aws:bad','x%2fhi','x?x=secret','x#x','..',''])
async def test_model_path_rejected(client,bad):
    with pytest.raises(HTTPException):
        upstream_request(provider(),{'upstream_model':bad},CHAT,'chat/completions')


@pytest.mark.parametrize('base', ['http://bedrock-runtime.us-east-1.amazonaws.com',
    'https://bedrock-runtime.us-east-1.amazonaws.com.evil.test',
    BASE+'/openai/v1',BASE+'?token=x','https://user:pass@bedrock-runtime.us-east-1.amazonaws.com',
    'https://169.254.169.254','https://bedrock-mantle.us-east-1.api.aws'])
async def test_native_origin_rejected(client,base):
    with pytest.raises(HTTPException):
        upstream_request(provider(base),ROUTE,CHAT,'chat/completions')


@pytest.mark.parametrize('extra', [{'stream':True},{'tools':[]},{'max_completion_tokens':4},{'reasoning_effort':'high'},
    {'response_format':{'type':'json_object'}},{'temperature':1.1},{'top_p':True},{'stop':['']},
    {'messages':[{'role':'user','content':[{'type':'text','text':'hi'}]}]},
    {'messages':[{'role':'user','content':'hi','name':'ignored'}]},
    {'messages':[{'role':'assistant','content':'hello'}]},
    {'messages':[{'role':'user','content':'hi'},{'role':'system','content':'late'}]}])
async def test_unsupported_request_rejected(client,extra):
    with pytest.raises(HTTPException):
        upstream_request(provider(),ROUTE,CHAT|extra,'chat/completions')


@pytest.mark.parametrize('reason,expected',[('end_turn','stop'),('stop_sequence','stop'),('max_tokens','length'),
                                          ('guardrail_intervened','content_filter'),('content_filtered','content_filter')])
async def test_response_contract(client,reason,expected):
    data = reply()
    data['stopReason'] = reason
    result = translate_response(provider(),data,'chat')
    assert result['choices'][0]['finish_reason'] == expected
    assert result['choices'][0]['message'] == {'role':'assistant','content':'hello'}
    assert result['model']=='chat' and result['usage']=={'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}
    assert 'metrics' not in result


@pytest.mark.parametrize('bad', [
    {'stopReason':'tool_use'}, {'usage':{}}, {'usage':{'inputTokens':True,'outputTokens':3,'totalTokens':4}},
    {'usage':{'inputTokens':10,'outputTokens':3,'totalTokens':99}},
    {'usage':{'inputTokens':10,'outputTokens':3,'totalTokens':13,'cacheReadInputTokens':1}},
    {'output':{'message':{'role':'assistant','content':[{'reasoningContent':{'text':'private'}}]}}},
    {'output':{'message':{'role':'assistant','content':[{'text':'hello','toolUse':{}}]}}},
    {'error':{'message':'secret'}}])
async def test_malformed_native_response_rejected(client,bad):
    with pytest.raises(ValueError):
        translate_response(provider(),reply()|deepcopy(bad),'chat')


async def test_dispatch_and_actual_usage(client,admin_headers):
    headers, _, _ = await native(client,admin_headers)
    seen = []
    async def handler(request):
        seen.append(request)
        assert request.headers['authorization']=='Bearer provider-secret'
        body=json.loads(request.content)
        assert body['inferenceConfig']['maxTokens']==64
        return httpx.Response(200,json=reply())
    await mock_upstream(handler)
    response=await client.post('/v1/chat/completions',headers=headers,json=CHAT)
    assert response.status_code==200,response.text
    assert len(seen)==1 and str(seen[0].url)==BASE+'/model/'+ROUTE['upstream_model']+'/converse'
    async with db.session_factory() as session:
        usage=(await session.scalars(select(db.Usage))).one()
        assert usage.input_tokens==10 and usage.output_tokens==3 and not usage.estimated
        assert usage.cost_micros==16


@pytest.mark.parametrize('extra', [{'stream':True},{'tools':[]},{'max_completion_tokens':3}])
async def test_reject_before_admission_or_dispatch(client,admin_headers,extra):
    headers, _, _ = await native(client,admin_headers)
    async def handler(request):
        pytest.fail('Unsupported request dispatched')
    await mock_upstream(handler)
    body={**CHAT,**extra}
    if 'max_completion_tokens' in extra:
        body.pop('max_tokens')
    result=await client.post('/v1/chat/completions',headers=headers,json=body)
    assert result.status_code==400,result.text
    async with db.session_factory() as session:
        for table in [db.Reservation,db.Usage,db.RateBucket]:
            assert await session.scalar(select(func.count()).select_from(table))==0


async def test_embeddings_not_routed(client,admin_headers):
    headers, _, _ = await native(client,admin_headers)
    async def handler(request):
        pytest.fail('Embedding dispatched to Converse')
    await mock_upstream(handler)
    response=await client.post('/v1/embeddings',headers=headers,json={'model':'chat','input':'hello'})
    assert response.status_code in {400,404,503}
    async with db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.Reservation))==0


async def test_malformed_response_is_sanitized_and_estimated(client,admin_headers):
    headers, _, _ = await native(client,admin_headers)
    async def handler(request):
        return httpx.Response(200,json={'output':'bedrock-secret private response'})
    await mock_upstream(handler)
    response=await client.post('/v1/chat/completions',headers=headers,json=CHAT)
    assert response.status_code==502,response.text
    assert 'bedrock-secret' not in response.text and 'private response' not in response.text
    async with db.session_factory() as session:
        usage=(await session.scalars(select(db.Usage))).one()
        assert usage.estimated


@pytest.mark.parametrize('failure',['stall','oversize','credential'])
async def test_native_transport_bounds_and_redaction(client,admin_headers,monkeypatch,failure):
    from gateway.config import get_settings
    from test_stream_limits import StalledStream, ScriptedStream
    from test_response_bounds import assert_settled
    headers, _, item = await native(client,admin_headers)
    if failure=='stall':
        monkeypatch.setattr(get_settings(),'provider_response_duration_seconds',.02)
        stream=StalledStream()
        response=httpx.Response(200,stream=stream)
    elif failure=='oversize':
        stream=ScriptedStream([b'x'*(16_777_216+1)])
        response=httpx.Response(200,stream=stream)
    else:
        stream=None
        response=httpx.Response(403,json={'message':'private bedrock-secret credential details'})
    await mock_upstream(lambda req:response)
    result=await client.post('/v1/chat/completions',headers=headers,json=CHAT)
    assert result.status_code==502
    assert 'bedrock-secret' not in result.text and 'credential details' not in result.text
    if stream:
        assert stream.closed
    if failure == 'credential':
        async with db.session_factory() as session:
            reservation = (await session.scalars(select(db.Reservation))).one()
            assert reservation.status == 'released'
            assert await session.scalar(select(func.count()).select_from(db.Usage)) == 0
    else:
        await assert_settled(item['id'])



async def test_native_cancellation_settles_and_closes(client,admin_headers):
    import asyncio
    from starlette.requests import Request
    from gateway import inference
    from gateway.main import app
    from test_stream_limits import StalledStream
    from test_response_bounds import assert_settled
    headers, _, item = await native(client,admin_headers)
    upstream=StalledStream()
    await mock_upstream(lambda req:httpx.Response(200,stream=upstream))
    async def receive():
        return {'type':'http.request','body':json.dumps(CHAT).encode(),'more_body':False}
    request=Request({'type':'http','method':'POST','path':'/v1/chat/completions','app':app,
                     'headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()]},receive)
    task=asyncio.create_task(inference.infer(request,'chat/completions'))
    await upstream.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert upstream.closed and upstream.cancelled.is_set()
    await assert_settled(item['id'],outcome=None)


async def test_admin_can_configure_bedrock_without_exposing_token(client,admin_headers):
    result=await client.post('/admin/api/providers',headers=admin_headers,json={
        'name':'Bedrock','kind':'bedrock_converse','base_url':BASE,'api_key':'bedrock-secret',
        'models':{'chat':{**ROUTE,'input_cost_per_million':1,'output_cost_per_million':2}}})
    assert result.status_code==201,result.text
    assert result.json()['kind']=='bedrock_converse' and 'bedrock-secret' not in result.text
    listed=await client.get('/admin/api/providers',headers=admin_headers)
    assert 'bedrock-secret' not in listed.text
