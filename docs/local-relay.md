# Local relay

The relay lets an SDK keep using one local token while the relay obtains short-lived central gateway access tokens. It does **not** rotate provider-owned API keys or remove the need to secure a bootstrap credential. The central administrator can issue it, or optional browser SSO enrollment below can create it without manually copying a root key; without the opt-in durable journal below, rotating it requires updating the relay environment and restarting it. These are gateway-specific exchange/enrollment protocols, not a claim of OAuth device-authorization standard compliance.

Run after installing the package:

```sh
export CENTRAL_GATEWAY_URL=https://gateway.example.com
# Supply these via your secret manager; do not put real values in shell history.
export RELAY_CREDENTIAL='<central-agent-credential>'
export RELAY_LOCAL_TOKEN='<distinct-random-local-token-at-least-32-characters>'
python -m gateway.relay
```

Configure SDK base URL `http://127.0.0.1:8090/v1` and API key equal to `RELAY_LOCAL_TOKEN`. Supported routes: chat completions (including SSE), the text-only Responses subset, embeddings and models. The local API token is mandatory. The root credential only goes to gateway agent enrollment/exchange/rotation endpoints, never to client code or the inference endpoint. Cached access tokens live only in memory, refresh before expiry, and concurrent calls share a single exchange. Revocation is enforced by the central gateway; an exchange failure fails closed rather than using an expired token.

Binding defaults to `127.0.0.1:8090`. A nonloopback `RELAY_BIND` requires `RELAY_ALLOW_REMOTE_BIND=true`. Do not expose the plain HTTP relay directly to a network. Put TLS and network access controls in front if remote access is required. There is no CORS support, generic URL forwarding, or unauthenticated inference route. `/health/live` only reports process liveness.

Requests are buffered up to 4 MiB; responses stream without being fully buffered. The relay strips client authentication and sends only its short-lived central access token. A 401 can trigger one token exchange and retry before any downstream bytes are sent. Timeouts, 5xx and streams that have started are never replayed, avoiding accidental duplicate inference/billing. Redirects are rejected. A dropped stream must be handled by the SDK as a failed/partial response.

Access logs are disabled by the relay CLI. Environment secrets are still accessible to the same OS account and privileged processes; use a dedicated user and your platform's secret manager. This first release does not integrate OS keychains, OIDC device flow, hardware-backed credentials or cloud-managed credential storage. Opt-in unattended root rollover is described below. See OAuth 2.0 Security Best Current Practice for production credential-lifecycle design: https://www.rfc-editor.org/rfc/rfc9700.html.

## Opt-in durable root-credential rotation (POSIX, schema revision 6)

The relay can now rotate its **long-lived gateway agent root credential**, not just
renew access JWTs. This is opt-in because it stores root secrets in a local durable
journal. It does not rotate provider API keys or extend gateway key expiry.

Create a private directory owned by the relay OS account, with mode `0700` and no
symlinks in its absolute path. On first startup set the issued agent credential in
`RELAY_CREDENTIAL`, plus `RELAY_JOURNAL_DIRECTORY=/absolute/private/directory`.
After the journal is initialized, its current credential is authoritative and
`RELAY_CREDENTIAL` may be removed. `RELAY_LOCAL_TOKEN` remains mandatory and distinct.
The journal is plaintext, mode `0600`; protect its disk and backups accordingly.
Do not put it on a shared network filesystem or copy it between devices. Native
Windows is unsupported for this opt-in feature. Default nonjournal behavior is
unchanged and keeps the configured root credential in memory.

Set `RELAY_ROTATION_INTERVAL_SECONDS` (600–2592000; default 86400). Rotation happens
on the next needed JWT exchange after the interval, not on an idle background
schedule. The singleflight token lock and a lifetime filesystem process lock allow
only one rotation process per journal. Issue **one agent credential per relay/device**;
using one credential with multiple journals can invalidate other devices.

### Crash-safe protocol

1. The client generates a new 256-bit secret and operation UUID and durably writes
   both alongside the current secret before any network transmission. Writes use
   a private temporary file, file fsync, atomic rename, and directory fsync.
2. Current-root-authenticated `POST /auth/agent/rotation/prepare` stores only the new
   credential's HMAC digest, operation ID, generation and a ten-minute pending TTL
   (never beyond root expiry). One pending operation is allowed per logical key.
3. Candidate-root-authenticated `POST /auth/agent/rotation/commit` atomically replaces
   the digest, increments generation and clears pending state. Key ID, permissions,
   allowlist, budgets, rate policy and expiry are preserved.
4. The client atomically marks the journal's candidate as current after a validated
   commit acknowledgment. Lost prepare/commit responses and restarts recover by
   retrying commit with the journaled candidate; an already committed operation
   returns the same logical outcome. Unknown pending credentials allow retrying
   prepare with the old root. Ambiguous failures retain both secrets and fail closed.

Old access JWTs are fenced by generation both at authentication and again at
admission; rotation does not cancel already dispatched provider work. Revocation,
expiry, disabled users/teams and membership changes remain effective during prepare,
commit and replay. A definitively expired pending operation is discarded locally,
while retaining the still-current root; a subsequent request starts a fresh operation.

### Operational recovery boundaries

Use a local filesystem that honors fsync/atomic rename and preserve the private
journal across service/container restarts. No implementation can recover a new
secret from server digests if the only journal copy is lost or restored from an old
backup after commit. In that case an administrator must issue a new device credential
and revoke the old logical key. Do not delete the journal to work around a transient
503: it may hold the only valid candidate after a lost response. Permission errors,
symlinks, another process holding the lock, storage failures or unexpected server
responses fail closed. Back up and inspect privately; never paste journal contents
into logs or support tickets. Automatic expiry extension and password-manager/OS
keychain integration are not provided.

## Optional SSO browser enrollment (schema revision 9)

Operators enable enrollment with an explicit nonempty model allowlist and finite
expiry/RPM/TPM policy. See [enrollment policy](enrollment.md). It is disabled by
default. No model request is made during enrollment.

```sh
mkdir -m 700 /absolute/private/relay-device
python -m gateway.enroll --gateway https://gateway.example.com \
  --journal /absolute/private/relay-device --name 'Workstation relay'
```

The command creates and fsyncs a random 256-bit root in the private journal
**before** sending it over TLS. It prints only a public approval code and the
browser page URL. Sign into SSO on that page, paste the code, review the device
name and policy, select an active team, and explicitly approve. A URL visit never
approves anything. Never approve a device you did not start yourself: a malicious
person can ask you to approve their device. Treat this as an access grant.

The command polls with its secret proof (five-second minimum). The server stores
only an HMAC digest, not the root. It atomically creates an inference-only agent
key bound to your selected team. The journal then records completion. Run:

```sh
export CENTRAL_GATEWAY_URL=https://gateway.example.com
export RELAY_JOURNAL_DIRECTORY=/absolute/private/relay-device
export RELAY_LOCAL_TOKEN='<separate-random-local-token-at-least-32-characters>'
# No RELAY_CREDENTIAL is needed for the enrolled journal.
python -m gateway.relay
```

The relay refuses a pending enrollment journal. The enrollment command and relay
cannot concurrently own the same journal. If a start/poll response is lost, rerun
the enrollment command using the **same directory**. Duplicate approval cannot
create duplicate keys or switch owner/team. The public approval expires after ten
minutes, but if approval already committed, secret-authenticated recovery can
recover the same active key even after the enrollment record was cleaned up.
Recovery requires matching original request ID, current root, live membership,
enabled user/team, and unexpired/unrevoked inference agent key. It never renews
expiry or revives access. No plaintext root appears in stdout or error details.

If an unapproved request expires, is denied, or policy changes before approval,
it cannot be approved later. Stop the old enrollment process, retain its journal
for investigation, and start a new enrollment in a new private directory. If
approval may have succeeded but recovery is denied, check/revoke that device in
the account portal before enrolling anew. Do not delete an uncertain journal or
create duplicate devices automatically. Existing manually provisioned journals
are not silently converted. No OS keychain integration is claimed.

Anonymous initiation is atomically capacity-bounded in the shared database, and
random polling proofs are rejected before acquiring the exclusive policy gate.
Put per-source rate limiting on enrollment endpoints at the ingress: bounded
storage alone cannot prevent attackers exhausting anonymous pending slots.
