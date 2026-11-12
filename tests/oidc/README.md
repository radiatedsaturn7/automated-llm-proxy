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

## Mounted installation-secret acceptance

This fixture runs both the migration and gateway production-mode process using
six allowlisted `*_FILE` settings, not direct secret environment variables.
The generator creates private `0600` source files containing newly generated
dummy values. A **fixture-only**, network-disabled root init container copies
these into a disposable named volume, owned by `root:10001`: directories `0550`,
files `0440`. `..data` and per-key symlinks reproduce the relevant Kubernetes
projection shape. App and migration mount the volume read-only and inherit the
image's nonroot UID/GID `10001:10001`. No secret is copied into an image layer.

Before actual browser/IdP flows, CI executes `check_mounted.py` inside the running
gateway: it asserts effective UID, symlinks, owners, groups and file permissions;
runs the application's read-only preflight; and confirms a subprocess rejects
conflicting direct environment plus mounted master-key input. Diagnostics only
print a fixed pass/fail message, never source values or exception details. The
ordinary verified-TLS Keycloak login/logout then exercises settings loaded from
those files, including the confidential client secret and session signing key.

The projection is created once and refuses to overwrite an existing generation.
Always remove the disposable stack with `down --volumes --remove-orphans` before
rerunning, and remove `generated/` afterward. CI performs both on success/failure.
This verifies container behavior, not a Kubernetes controller, secret-manager
integration, hot reload or coordinated multi-replica secret rotation. The root
init container is test infrastructure only; do not copy its elevated permissions
into a production deployment. Production Helm projects an existing versioned
Secret directly and does not need this init service.
