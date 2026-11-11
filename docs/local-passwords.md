# Optional local password login

SSO is the default. Local authentication is disabled unless `LOCAL_PASSWORD_ENABLED=true`.
No default credentials, signup, email reset or automatic email-account linking exist.
Federated OIDC/SCIM accounts cannot use local passwords, even if a credential was
provisioned before federation was linked.

Administrators provision/reset an existing nonfederated user with
`PUT /admin/api/users/{id}/password` and JSON `{"password":"a unique long passphrase"}`.
Use an administrator API credential or a browser session with its CSRF header.
Passwords must be 15–128 characters. Deliver initial credentials through your approved
secret-sharing channel, never repository configuration. Reset revokes all user browser
sessions. `DELETE /admin/api/users/{id}/password` removes the local credential and revokes
sessions; this emergency revocation works even when local login is disabled.

Users open `/admin/static/local-login.html`. Login requires JSON and an exact PUBLIC_URL
Origin. Sessions have HttpOnly, SameSite=Lax cookies, Secure with HTTPS; mutations use
the existing session CSRF checks. `LOCAL_SESSION_SECONDS` defaults to 3600 (300–28800).
Disabling the flag stops new logins, not existing sessions: revoke credentials/sessions
explicitly for immediate removal.

Passwords use salted scrypt N=32768/r=8/p=3. Unknown users perform a dummy derivation;
failures do not disclose account existence. At most two derivations run per replica,
with overload rejection. Shared database throttles permit five attempts per account
and thirty per source IP per 15-minute window. Throttles survive replica changes and
are committed before verification. Fixed 4096 hash buckets per namespace cap storage;
collisions conservatively share limits. Successful attempts also count. The socket
peer address is used, not untrusted forwarding headers. Configure the deployment's
trusted proxy handling correctly; a shared ingress address conservatively shares limits.

Limitations: no local MFA, breached-password service, self-service change or recovery,
or forced first-login reset. For workforce production authentication use an MFA-enforcing
identity provider. Admin-managed local accounts are an optional fallback, not an SSO substitute.

Upgrade safety: revision 10 conservatively marks all pre-existing users as managed,
because historical SCIM users may lack an external ID. Their API/SSO behavior is
unchanged, but they cannot gain a local password. Create a new, explicit local user
for fallback login. New SCIM-created or SCIM-managed accounts are marked managed
and any previous local credential is erased. There is no automatic unlink/bypass.
