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

Limitations: no phishing-resistant local MFA, breached-password service, self-service recovery,
or forced first-login reset. For workforce production authentication use an MFA-enforcing
identity provider. Admin-managed local accounts are an optional fallback, not an SSO substitute.

Upgrade safety: revision 10 conservatively marks all pre-existing users as managed,
because historical SCIM users may lack an external ID. Their API/SSO behavior is
unchanged, but they cannot gain a local password. Create a new, explicit local user
for fallback login. New SCIM-created or SCIM-managed accounts are marked managed
and any previous local credential is erased. There is no automatic unlink/bypass.

Members can change their password from `/portal`, supplying the current password
and a fresh enrolled TOTP code. This revokes all browser sessions and pending
MFA challenges but preserves the active factor and API keys. See
[member self-service](self-service.md#account-security).

## Optional local TOTP MFA (schema revision 12)

`LOCAL_MFA_POLICY=optional` is the compatibility default. Unenrolled local users
can sign in with their password; **once enrolled, TOTP is always required**, even
in optional mode. `LOCAL_MFA_POLICY=required` denies local password-only login and
immediately denies authorization by existing password-only browser sessions.
OIDC sessions are unaffected: enforce MFA at your identity provider. API keys
remain independent scoped credentials, not interactive MFA sessions.

Administrators provision a local password as above, then call
`POST /admin/api/users/{user_id}/mfa/setup-grant`. Deliver the returned one-time
`setup_grant` privately to the verified user, never in a URL. It expires in five
minutes. A new grant invalidates the user's earlier grant/pending enrollment.
The user sends JSON `{email,password,setup_grant}` with the deployment's exact
Origin to `POST /auth/local/mfa/enroll/start`. This consumes the grant and returns
an opaque challenge plus a base32 secret and `otpauth_uri`, shown once. Add it to
an authenticator and submit `{challenge,code}` to `POST /auth/local/mfa/verify`.
A successful code activates the factor, revokes old browser sessions and issues
a fresh session. There is no session merely for knowing the password or grant.

After enrollment, `POST /auth/local/login` returns `mfa_required`, `challenge`
and `expires_in` rather than a cookie. Complete the same verify endpoint. Code
failures allow at most five attempts; shared database throttles also bound user
and IP attempts. Only one challenge exists per user; starting another login
invalidates the previous challenge. Challenges expire after five minutes and
are bound to the current password. Password reset/revocation invalidates all
pending grants/challenges and sessions **without removing an enrolled factor**.

TOTP uses RFC 6238 SHA-1, six digits and 30-second periods, accepting one step
on either side for clock drift. Each accepted counter must be strictly newer
than the user's previous counter, atomically serialized across replicas. A code
cannot be used for another login after enrollment; wait for a newer time step.
Keep host clocks synchronized. These are phishable OTPs, not phishing-resistant
hardware MFA, and this feature makes no compliance or assurance certification.

Lost authenticators require an authorized administrator's explicit
`DELETE /admin/api/users/{user_id}/mfa`. This deletes the factor **and the local
password**, revokes all browser sessions and requires fresh password provisioning
and enrollment. There are no recovery codes, email recovery, remembered devices
or silent fallback. Maintain a protected, scoped administrative API credential
for break-glass recovery; protect and audit this privilege. Reset does not revoke
independent API keys: revoke those separately if compromise is suspected.

Seeds, including pending enrollment seeds, are encrypted with
`GATEWAY_MASTER_KEY`; grants/challenges are stored only as keyed digests. Factor
state has at most one row per administrator-provisioned user. Maintenance clears
expired transient fields in bounded batches, never active seeds/replay counters.
Preflight checks both active and pending seed decryption. Backups and master-key
escrow must preserve these secrets together; rotation without re-encryption will
break login and is not automatically performed. Audit entries contain operation
names and user IDs, never the seed, password, grant or code.
