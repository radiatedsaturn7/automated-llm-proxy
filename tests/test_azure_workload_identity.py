"""Synthetic workload-identity contracts; no live Azure credentials or calls."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gateway import azure_identity, db
from gateway.config import get_settings
from gateway.main import app
from gateway.schemas import ProviderCreate
from gateway.providers import upstream_request
from test_gateway import setup_inference, mock_upstream

BASE = 'https://my-resource.openai.azure.com/openai/v1'
CHAT = {'model':'chat','messages':[{'role':'user','content':'hi'}],'max_tokens':32}


class Credential:
    def __init__(self):
        self.calls = []
        self.closed = False
    async def get_token(self, *scopes):
        self.calls.append(scopes)
        return SimpleNamespace(token='synthetic-access-token', expires_on=time.time()+3600)
    async def close(self):
        self.closed = True


def enable(monkeypatch):
    monkeypatch.setenv('AZURE_WORKLOAD_IDENTITY_ENABLED', 'true')
    monkeypatch.setenv('AZURE_WORKLOAD_IDENTITY_ALLOWED_ENDPOINTS',json.dumps([BASE]))
    get_settings.cache_clear()
    credential=Credential()
    app.state.azure_workload_credential=credential
    return credential


@pytest.mark.parametrize('url', ['http://my-resource.openai.azure.com/openai/v1', BASE+'/', BASE+'?x=1',
    BASE+'#x', BASE.replace('my-resource.', 'my-resource.openai.azure.com.evil.'),
    BASE.replace('my-resource.', 'USER@my-resource.'), BASE.replace('.com/', '.com:443/'),
    'https://127.0.0.1/openai/v1', 'https://my-resource.openai.azure.com/other',
    'https://MY-RESOURCE.openai.azure.com/openai/v1', 'https://*.openai.azure.com/openai/v1',
    'https://my-resource.openai.azure.com/openai/v1?'])
def test_exact_endpoint_validation(url):
    assert not azure_identity.valid_endpoint(url)


@pytest.mark.parametrize('raw', ['{}','null','[1]',json.dumps([BASE,BASE]),'[]','broken'])
def test_invalid_enabled_config(raw):
    with pytest.raises(ValueError):
        azure_identity.validate_endpoint_list(raw,True)


async def test_default_off(client):
    assert azure_identity.create_credential(get_settings()) is None
    with pytest.raises(HTTPException):
        azure_identity.check_endpoint(BASE)


async def test_sdk_explicit_chain(client,monkeypatch):
    import azure.identity.aio
    seen={}
    def factory(**kwargs):
        seen.update(kwargs)
        return Credential()
    monkeypatch.setattr(azure.identity.aio,'WorkloadIdentityCredential',factory)
    enable(monkeypatch)
    assert isinstance(azure_identity.create_credential(get_settings()),Credential)
    assert seen['authority']=='https://login.microsoftonline.com'
    assert seen['retry_total']==0 and seen['logging_enable'] is False
    assert seen['transport']._use_env_settings is False
    await seen['transport'].close()


async def test_schema_key_rules():
    params={'name':'test','base_url':BASE,'kind':azure_identity.KIND,
            'models':{'chat':{'upstream_model':'deployment','input_cost_per_million':1,'output_cost_per_million':2}}}
    assert ProviderCreate(**params).api_key is None
    with pytest.raises(ValueError):
        ProviderCreate(**params,api_key='must-not-store')
    with pytest.raises(ValueError):
        ProviderCreate(**(params|{'kind':'azure_openai'}))


async def test_prepared_request_has_no_secret(client,monkeypatch):
    enable(monkeypatch)
    provider=SimpleNamespace(kind=azure_identity.KIND,base_url=BASE,encrypted_key='invalid ciphertext')
    url,headers,body=upstream_request(provider,{'upstream_model':'deploy'},CHAT|{'stream':True},'chat/completions')
    assert url==BASE+'/chat/completions' and headers=={'Content-Type':'application/json'}
    assert body['stream_options']=={'include_usage':True}
    assert body['model']=='deploy'


async def native(client,admin_headers,monkeypatch):
    headers, key, old=await setup_inference(client,admin_headers)
    credential=enable(monkeypatch)
    async with db.transaction() as s:
        provider=await s.get(db.Provider,old['id'])
        provider.kind,provider.base_url=azure_identity.KIND,BASE
    return headers,credential,old


async def test_inference_token_and_usage(client,admin_headers,monkeypatch):
    headers,credential,_=await native(client,admin_headers,monkeypatch)
    async def handler(request):
        assert request.headers['authorization']=='Bearer synthetic-access-token'
        assert 'api-key' not in request.headers
        assert str(request.url)==BASE+'/chat/completions'
        return httpx.Response(200,json={'id':'test','object':'chat.completion','model':'deploy',
            'choices':[{'index':0,'message':{'role':'assistant','content':'hello'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}})
    await mock_upstream(handler)
    r=await client.post('/v1/chat/completions',headers=headers,json=CHAT)
    assert r.status_code==200,r.text
    assert credential.calls==[(azure_identity.SCOPE,)]
    async with db.session_factory() as s:
        usage=(await s.scalars(select(db.Usage))).one()
        assert usage.cost_micros==16 and not usage.estimated


async def test_auth_failure_before_accounting(client,admin_headers,monkeypatch):
    headers,credential,_=await native(client,admin_headers,monkeypatch)
    async def fail(*args):
        raise RuntimeError('secret-token-never-reflect')
    credential.get_token=fail
    async def handler(request):
        pytest.fail('No dispatch expected')
    await mock_upstream(handler)
    r=await client.post('/v1/chat/completions',headers=headers,json=CHAT)
    assert r.status_code==503 and 'secret-token' not in r.text
    async with db.session_factory() as s:
        assert not (await s.scalars(select(db.Usage))).all()
        assert not (await s.scalars(select(db.Reservation))).all()


async def test_registration_policy_and_key_patch(client,admin_headers,monkeypatch):
    body={'name':'wi','kind':azure_identity.KIND,'base_url':BASE,
          'models':{'chat':{'upstream_model':'deploy','input_cost_per_million':1,'output_cost_per_million':2}}}
    assert (await client.post('/admin/api/providers',headers=admin_headers,json=body)).status_code==400
    enable(monkeypatch)
    r=await client.post('/admin/api/providers',headers=admin_headers,json=body)
    assert r.status_code==201,r.text
    r=await client.patch('/admin/api/providers/'+r.json()['id'],headers=admin_headers,json={'api_key':'no'})
    assert r.status_code==400


async def test_revalidate_allowlist_before_token(client,monkeypatch):
    credential=enable(monkeypatch)
    with pytest.raises(HTTPException):
        await azure_identity.authorization(app,'https://other.openai.azure.com/openai/v1')
    assert credential.calls==[]


@pytest.mark.parametrize('token', ['', 'has\r\nnewline','a'*32769,'unicode-☃'])
async def test_bad_tokens_sanitized(client,monkeypatch,token):
    credential=enable(monkeypatch)
    async def get(*args):
        return SimpleNamespace(token=token,expires_on=time.time()+3600)
    credential.get_token=get
    with pytest.raises(HTTPException) as exc:
        await azure_identity.authorization(app,BASE)
    assert exc.value.status_code==503


async def test_cancel_propagates_and_close(client,monkeypatch):
    credential=enable(monkeypatch)
    async def get(*args):
        raise asyncio.CancelledError()
    credential.get_token=get
    with pytest.raises(asyncio.CancelledError):
        await azure_identity.authorization(app,BASE)
    await azure_identity.close_credential(app)
    assert credential.closed and app.state.azure_workload_credential is None


async def test_timeout_is_bounded_and_no_reservation(client,admin_headers,monkeypatch):
    headers,credential,_=await native(client,admin_headers,monkeypatch)
    get_settings().azure_identity_timeout_seconds=.01
    cancelled=[]
    async def slow(*args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    credential.get_token=slow
    r=await client.post('/v1/chat/completions',headers=headers,json=CHAT)
    assert r.status_code==503 and cancelled==[True]
    async with db.session_factory() as s:
        assert not (await s.scalars(select(db.Reservation))).all()


async def test_real_sdk_constructor_missing_env_sanitized(client,monkeypatch):
    enable(monkeypatch)
    for name in ('AZURE_TENANT_ID','AZURE_CLIENT_ID','AZURE_FEDERATED_TOKEN_FILE'):
        monkeypatch.delenv(name,raising=False)
    with pytest.raises(RuntimeError,match='deployment configuration is invalid'):
        azure_identity.create_credential(get_settings())


async def test_sdk_reads_only_operator_env(client,monkeypatch,tmp_path):
    enable(monkeypatch)
    monkeypatch.setenv('AZURE_TENANT_ID','00000000-0000-0000-0000-000000000001')
    monkeypatch.setenv('AZURE_CLIENT_ID','00000000-0000-0000-0000-000000000002')
    monkeypatch.setenv('AZURE_FEDERATED_TOKEN_FILE',str(tmp_path/'synthetic-token'))
    monkeypatch.setenv('AZURE_AUTHORITY_HOST','https://attacker.invalid')
    credential=azure_identity.create_credential(get_settings())
    assert credential._token_file_path==str(tmp_path/'synthetic-token')
    assert credential._client._authority=='https://login.microsoftonline.com'
    await credential.close()


async def test_lifespan_partial_startup_closes_identity(client,monkeypatch):
    from gateway import main
    credential=Credential()
    monkeypatch.setattr(azure_identity,'create_credential',lambda cfg:credential)
    def fail(**kwargs):
        raise RuntimeError('HTTP startup failure')
    monkeypatch.setattr(main.httpx,'AsyncClient',fail)
    with pytest.raises(RuntimeError,match='HTTP startup failure'):
        async with main.lifespan(app):
            pytest.fail('Should never start')
    assert credential.closed and app.state.azure_workload_credential is None


async def test_lifespan_identity_failure_does_not_allocate_http(client,monkeypatch):
    from gateway import main
    def fail(cfg):
        raise RuntimeError('invalid deployment')
    monkeypatch.setattr(azure_identity,'create_credential',fail)
    monkeypatch.setattr(main.httpx,'AsyncClient',lambda **kwargs:pytest.fail('no HTTP allocation'))
    with pytest.raises(RuntimeError,match='invalid deployment'):
        async with main.lifespan(app):
            pytest.fail('Should never start')


async def test_lifespan_closes_current_http_client(client):
    from fastapi import FastAPI
    from gateway.main import lifespan
    instance=FastAPI()
    replacement=SimpleNamespace(closed=False)
    async def close():
        replacement.closed=True
    replacement.aclose=close
    async with lifespan(instance):
        await instance.state.upstream.aclose()
        instance.state.upstream=replacement
    assert replacement.closed
