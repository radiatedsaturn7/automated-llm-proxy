# Disposable real OIDC CI fixture

`real-oidc` in the CI workflow starts official pinned Keycloak 26.4.0, Nginx
1.27.4 and disposable PostgreSQL. This is an isolated test deployment, not a
production Keycloak recommendation. No live organization IdP, real users or paid
model providers are contacted. The test realm has generated dummy credentials
and no registration, password reset or direct password grant.

`generate.py` creates one-day TLS material plus a confidential gateway client
and two dummy users (one mapped administrator, one with no authorized group).
The Keycloak client requires S256 PKCE. Nginx gives both the browser and gateway
consistent `https://idp.test` issuer URLs over port 443 and serves the gateway at
`https://gateway.test`. The generated trust root is added to the test gateway
image's certifi bundle, Chromium's NSS trust store and Keycloak's truststore.
Node API requests use `NODE_EXTRA_CA_CERTS`. No TLS verification is disabled and
no application auth/discovery/JWKS function is monkeypatched.

The dedicated Playwright configuration runs two tests: real browser login and
verified-email/group mapping, cookie security flags, CSRF rejection/acceptance,
local logout followed by SSO, real Keycloak logout with its signed back-channel
notification, and denial of the authenticated but unauthorized user. These are
real protocol interactions but **not** vendor certification, MFA policy testing,
JWKS rotation testing, or proof against every IdP interoperability failure.
Only successful CI logs establish execution; local syntax/configuration checks
are not a substitute.

The Compose stack has a fixed CI project name, no published database/IdP backend
ports, and only loopback HTTPS exposed. CI imposes a 15-minute job timeout and a
bounded readiness loop. Access logging is off; browser trace/screenshots/video
are disabled. Generated credentials are masked before being exported to the CI
environment. Do not enable verbose HTTP/debug logging or upload the generated
realm, environment or TLS private key. Cleanup removes containers and generated
files, including on failure. The official identity image and public package
registries are the only additional infrastructure downloads.

Never reuse this realm, generated secrets, trusted CA, or test proxy in a real
deployment. The fixture intentionally uses Keycloak's `start-dev` and internal
HTTP behind the private TLS proxy; its acceptance boundary is external TLS and
actual OIDC, not production IdP operations. Browser user credentials are synthetic
and the page only reaches `.test` hostnames mapped to the CI runner.
