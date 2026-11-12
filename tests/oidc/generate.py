"""Generate disposable CI-only identity data; never print or retain credentials."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parent
out = ROOT / 'generated'
out.mkdir(mode=0o700, exist_ok=True)
# Directory readable for mounted test-only certificates/realm; no host users on CI.
out.chmod(0o755)
password = secrets.token_urlsafe(32)
client_secret = secrets.token_urlsafe(40)
db_password = secrets.token_urlsafe(32)
values = {
    'POSTGRES_PASSWORD': db_password,
    'DATABASE_URL': f'postgresql+asyncpg://gateway:{db_password}@postgres:5432/gateway',
    'GATEWAY_MASTER_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    'GATEWAY_SIGNING_KEY': secrets.token_urlsafe(48),
    'BOOTSTRAP_TOKEN': secrets.token_urlsafe(48),
    'METRICS_TOKEN': secrets.token_urlsafe(48),
    'OIDC_CLIENT_SECRET': client_secret,
    'OIDC_TEST_PASSWORD': password,
}
with (out / 'fixture.env').open('w') as f:
    for name, value in values.items():
        f.write(f'{name}={value}\n')
(out / 'fixture.env').chmod(0o600)
if os.environ.get('GITHUB_ENV'):
    with open(os.environ['GITHUB_ENV'], 'a') as f:
        for name, value in values.items():
            print(f'::add-mask::{value}')
            f.write(f'{name}={value}\n')
realm = {
    'realm':'gateway-ci', 'enabled':True, 'sslRequired':'external',
    'registrationAllowed':False, 'resetPasswordAllowed':False,
    'groups':[{'name':'gateway-admins'}],
    'clients':[{'clientId':'gateway','enabled':True,'protocol':'openid-connect',
        'publicClient':False,'secret':client_secret,'standardFlowEnabled':True,
        'directAccessGrantsEnabled':False,
        'redirectUris':['https://gateway.test/auth/oidc/callback'],
        'webOrigins':['https://gateway.test'],
        'attributes':{'pkce.code.challenge.method':'S256',
          'post.logout.redirect.uris':'https://gateway.test/portal',
          'backchannel.logout.url':'https://gateway.test/auth/oidc/backchannel-logout',
          'backchannel.logout.session.required':'true'},
        'protocolMappers':[{'name':'groups','protocol':'openid-connect',
            'protocolMapper':'oidc-group-membership-mapper',
            'config':{'claim.name':'groups','full.path':'false','id.token.claim':'true',
                      'access.token.claim':'false','userinfo.token.claim':'true'}}]}],
    'users':[{'username':name,'email':name+'@example.test','emailVerified':True,
        'firstName':'CI','lastName':'Fixture','enabled':True,
        'groups':['gateway-admins'] if name=='admin-fixture' else [],
        'credentials':[{'type':'password','value':password,'temporary':False}]}
        for name in ['admin-fixture','outsider-fixture']],
}
(out / 'realm.json').write_text(json.dumps(realm))
(out / 'realm.json').chmod(0o644)  # Import by unprivileged test container.
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
    '-keyout',str(out/'tls.key'),'-out',str(out/'tls.crt'),'-subj','/CN=Thunderbolt CI only',
    '-addext','subjectAltName=DNS:idp.test,DNS:gateway.test',
    '-addext','basicConstraints=critical,CA:TRUE'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
(out/'tls.key').chmod(0o600)
(out/'tls.crt').chmod(0o644)
