# Security model and deployment checklist

This project has not undergone an independent security audit. Do not interpret included controls or deployment examples as a compliance certification.

## Trust boundaries

Administrators can configure upstream destinations and obtain access to organization-wide usage metadata. Treat administrator access as privileged. Provision the first administrator with the one-time bootstrap flow on an empty database; the bootstrap secret is not an ongoing administrator API key. Store bootstrap material in a secret manager and remove it from routine operator workflows after setup.

Gateway keys are stored as keyed digests, not plaintext. Provider credentials are encrypted with the configured master key. Keep encryption and signing keys outside source control, container images, logs and Terraform state. Database backups still contain sensitive identity, usage and session data and require encryption and access control. Losing encryption/signing material is an operational incident; no automatic multi-key rotation migration is supplied.

OIDC delegates authentication, password policy and MFA to an identity provider. Group mappings are privileged configuration: map only explicit trusted groups to administrator access. Validate issuer, client, redirect URI and membership behavior in your own IdP before deployment. The supported SCIM Users/Groups subset revokes local sessions and keys during deactivation and membership changes; it is not complete SCIM or IdP interoperability certification. Secondary team memberships require separate reconciliation. See [provisioning](provisioning.md). SAML is not implemented; use OIDC-capable identity providers.

## Browser and credential controls

The admin UI does not persist manual keys in localStorage or sessionStorage and renders data as text, not HTML. Newly created credentials display once and are cleared from the dialog after two minutes; clipboard contents are controlled by the OS and are not erased by closing the dialog. Use OIDC for routine administration rather than sharing privileged keys.

Serve the gateway only over HTTPS in production. Cookies must remain Secure, HttpOnly and SameSite, and mutation requests using browser sessions must carry the session CSRF token. Keep the content security policy and security headers enabled at ingress and application layers. Never embed administration into an untrusted site. Restrict management and metrics endpoints at the network layer as defense in depth.

## Provider and request risks

Provider URLs are privileged configuration, not user-supplied forward-proxy destinations. Do not enable private provider hosts unless required for a controlled internal model service. Enforce egress policy independently: application URL validation is not a replacement for DNS, routing and firewall protections against SSRF or compromised providers. Use dedicated provider credentials with minimal permissions and spending caps.

A model response and tool-call arguments are untrusted data. This gateway does not execute tool calls and does not guarantee prompt-injection prevention, factual correctness or absence of sensitive data. Deterministic evaluation is not a safety filter. Opt-in inbound block/redact guardrails provide bounded, reviewed heuristic detectors, not comprehensive DLP. They do not inspect provider outputs or reliably detect encoded or obfuscated secrets. See [guardrails](guardrails.md). If you need broader PII/DLP coverage, moderation or output approval, integrate and validate those separately before allowing sensitive workloads.

Default audit/usage records should contain metadata, not prompt or response bodies. Reverse proxies, tracing agents and providers have independent logging policies; inspect them before sending confidential material. Audit rows are not tamper-evident or WORM-protected. Define retention, export and erasure procedures rather than allowing unbounded growth.

## Before production

- Run tests and independent review, dependency/image scanning, secret scanning and abuse tests.
- Use PostgreSQL with TLS, restricted accounts, connection limits, backups and tested restore/failover.
- Rotate example secrets; configure exact public HTTPS URL and trusted OIDC groups.
- Restrict ingress/egress, management access and token-protected metrics. Configure request/time limits at ingress too.
- Validate budgets against provider invoices, failure cases and concurrent traffic. Reconcile pending reservations only after checking billing.
- Load-test representative streaming and nonstreaming requests, database lock contention and pod termination.
- Exercise revoked/expired keys, team disabling, logout, session expiry and IdP membership changes.
- Define incident response, credential revocation, audit retention and upgrade/rollback procedures.

Report vulnerabilities privately to repository maintainers using GitHub private vulnerability reporting if enabled. Do not post live secrets or exploitable production details in a public issue.

## Explicit identity linking and session response

An administrator can link a pre-provisioned user using `POST /admin/api/users/{id}/identity` with `issuer`, `subject` and `identity_verified: true`. Obtain the subject from the trusted IdP administration console, not an email or unverified user claim. The issuer must exactly match this deployment's OIDC issuer. Linking is audited, rejects subjects belonging to another user and rejects replacement of an existing different link. It revokes that user's existing browser sessions on first linking; keys are unchanged. No automatic email-based linking occurs. SSO still requires an authorized group and its mapping determines role and team at login.

`POST /admin/api/users/{id}/sessions/revoke` revokes one user's browser sessions; `POST /admin/api/sessions/revoke-all` revokes everyone's, including the caller's cookie session. Both require administrator authorization and JSON `{"confirm":true}` and create audit events. They do not revoke API/agent keys, prevent new IdP login, or end IdP sessions. Disable the user and separately revoke credentials for account containment. The admin panel exposes these actions with explicit confirmations. After revoking your own browser session, sign in again before further administration.

## OIDC compatibility and cache policy

`OIDC_GROUP_CLAIM` selects a dot-separated object path (default `groups`, for example `realm_access.roles`). Its final value must be an array of strings; missing, malformed or ambiguous authorization mappings fail closed. Email remains the standard verified `email` claim; configurable paths do not bypass `email_verified` checks. Group mapping is evaluated at login, not continuously against the IdP. Local account disabling/revocation takes effect immediately, but IdP group changes require new login or explicit local session revocation.

`OIDC_SESSION_SECONDS` controls absolute local browser-session lifetime, from 300 to 86400 seconds (default 28800). Browser logout revokes the local session, not the IdP session. No refresh token is stored and live group refresh is not claimed. The signed IdP back-channel logout endpoint described below handles notifications from a configured provider separately.

Discovery and public signing keys are cached in replica memory for `OIDC_CACHE_SECONDS` (10–3600 seconds; default 300), with monotonic expiration and serialized async refresh. Expired metadata is never reused after a failed fetch. An unknown signing-key ID triggers one immediate JWKS refresh, limited to once per ten seconds per replica/issuer; another new key during that cooldown may require retry. Key rotation reusing the same key ID takes effect after TTL expiry. Emergency key removal can therefore take up to the TTL per replica; clear/restart replicas when required. No synchronous network calls occur in the OIDC path.

Discovery metadata endpoints must use HTTPS on port 443, without credentials/fragments, and have the issuer's exact origin. For IdPs with separate endpoint origins, explicitly configure `OIDC_TRUSTED_ENDPOINT_ORIGINS` as a JSON array such as `["https://login.example.com"]`. Never add an origin merely because untrusted metadata requests it. This is especially important because the token endpoint receives the client secret. Redirects, private IP literals, localhost and oversized responses are rejected. DNS/egress enforcement remains necessary against DNS rebinding or compromised trusted hosts; origin checks alone do not guarantee network isolation.

## Local password fallback

Local login is optional and disabled by default (`LOCAL_PASSWORD_ENABLED=false`).
Prefer SSO with your IdP's MFA and password policy. Administrator-provisioned local
passwords use salted scrypt; neither plaintext passwords nor submitted login bodies
belong in audit records. There is no public registration or email reset flow.
OIDC-linked and SCIM-managed identities cannot log in with local passwords, even if
a password record predates federation. This prevents fallback from bypassing SSO
membership policy. Account/team disablement still applies.

Login requires the configured public origin. Shared database throttles apply before
password derivation; bounded hashed buckets intentionally share limits on collision.
The socket address, not an untrusted forwarded header, supplies the IP limit. Behind
a reverse proxy, clients may share that limit; test proxy behavior before enabling
password login. Bounded password derivation rejects excess concurrent work rather
than consuming unlimited memory. Per-replica concurrency limits are resource controls,
not a replacement for distributed throttling or ingress abuse protections.

Administrator reset/revoke invalidates the user's browser sessions, not independent
API keys or agent credentials. Local authentication does not provide MFA, compromised
password screening, a first-login change requirement or self-service email recovery.
Do not claim equivalent protection to an MFA-enforced IdP. See [local passwords](local-passwords.md).

## Signed IdP back-channel logout

Register `PUBLIC_URL/auth/oidc/backchannel-logout` with the configured identity provider.
It accepts one URL-encoded `logout_token`, signed by the same trusted RS256/ES256 JWKS
used for login. Issuer, audience, event, identifiers and issuance time are checked;
nonce-bearing tokens are rejected. A subject or session ID is required. When both
are supplied both must match. Local sessions and independent API/agent credentials
are not revoked by this endpoint; disable users/revoke credentials separately for
full offboarding. No remote provider or vendor interoperability certification is implied.

A shared database replay record makes duplicate notifications idempotent without
revoking newer sessions. Issuer/subject/session tombstones also prevent a concurrent
OIDC callback from recreating a session from an older ID token. Both login ID tokens
and logout tokens must have integer issuance times within the last 300 seconds;
replica/IdP clocks must be synchronized. Tombstones are retained for 600 seconds.
A fresh login issued in the same second as logout is conservatively rejected: retry
a new sign-in. Failure to retrieve trusted signing metadata fails closed.

Revision 11 invalidates all pre-upgrade browser sessions once, because they do not
contain trustworthy IdP session correlation. Schedule the upgrade and notify users
to sign in again. API credentials are not changed by that migration.
