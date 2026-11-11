# Security model and deployment checklist

This project has not undergone an independent security audit. Do not interpret included controls or deployment examples as a compliance certification.

## Trust boundaries

Administrators can configure upstream destinations and obtain access to organization-wide usage metadata. Treat administrator access as privileged. Provision the first administrator with the one-time bootstrap flow on an empty database; the bootstrap secret is not an ongoing administrator API key. Store bootstrap material in a secret manager and remove it from routine operator workflows after setup.

Gateway keys are stored as keyed digests, not plaintext. Provider credentials are encrypted with the configured master key. Keep encryption and signing keys outside source control, container images, logs and Terraform state. Database backups still contain sensitive identity, usage and session data and require encryption and access control. Losing encryption/signing material is an operational incident; no automatic multi-key rotation migration is supplied.

OIDC delegates authentication, password policy and MFA to an identity provider. Group mappings are privileged configuration: map only explicit trusted groups to administrator access. Validate issuer, client, redirect URI and membership behavior in your own IdP before deployment. Browser-session expiry and local user disabling are not a complete SCIM deprovisioning integration. Local password accounts, SAML, SCIM and IdP back-channel logout are not implemented.

## Browser and credential controls

The admin UI does not persist manual keys in localStorage or sessionStorage and renders data as text, not HTML. Newly created credentials display once and are cleared from the dialog after two minutes; clipboard contents are controlled by the OS and are not erased by closing the dialog. Use OIDC for routine administration rather than sharing privileged keys.

Serve the gateway only over HTTPS in production. Cookies must remain Secure, HttpOnly and SameSite, and mutation requests using browser sessions must carry the session CSRF token. Keep the content security policy and security headers enabled at ingress and application layers. Never embed administration into an untrusted site. Restrict management and metrics endpoints at the network layer as defense in depth.

## Provider and request risks

Provider URLs are privileged configuration, not user-supplied forward-proxy destinations. Do not enable private provider hosts unless required for a controlled internal model service. Enforce egress policy independently: application URL validation is not a replacement for DNS, routing and firewall protections against SSRF or compromised providers. Use dedicated provider credentials with minimal permissions and spending caps.

A model response and tool-call arguments are untrusted data. This gateway does not execute tool calls and does not guarantee prompt-injection prevention, factual correctness or absence of sensitive data. Deterministic evaluation is not a safety filter. If you need PII redaction, DLP, moderation or output approval, integrate and validate those separately before allowing sensitive workloads.

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
