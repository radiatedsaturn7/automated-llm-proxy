# SSO-assisted relay enrollment

Enrollment is opt-in. It lets an authenticated user explicitly authorize a
local relay without copying a root credential through the browser. It is not
OAuth Device Authorization Grant interoperability or hardware attestation.
The relay generates and durably saves its secret before starting enrollment;
the server stores digests. The browser receives only a server-generated opaque UUID approval
code and policy metadata, never the relay root secret. The approval code is
separate from the relay’s internal request identifier.

## Operator configuration

Configure SSO first and give users active team membership. Define finite model
aliases; enrollment cannot grant administration, wildcard model access or
unbounded expiration. The Helm chart exposes:

```yaml
enrollment:
  enabled: true
  allowedModels: [chat, embeddings]
  keyDays: 30
  rpm: 60
  tpm: 100000
  maxPending: 1000
```

Equivalent environment names are `ENROLLMENT_ENABLED` (default false),
`ENROLLMENT_ALLOWED_MODELS` (JSON array, empty default), `ENROLLMENT_KEY_DAYS`
(1–90, default 30), `ENROLLMENT_RPM` (1–10,000, default 60), `ENROLLMENT_TPM`
(1–10,000,000, default 100,000), and `ENROLLMENT_MAX_PENDING` (1–10,000,
default 1,000). Enabled policy requires a nonempty model list, at most 100 names
of at most 128 characters each. Policy settings must match on every replica;
roll out changes consistently. Avoid duplicating these variables in `extraEnv`.
Pending requests expire after ten minutes; polling interval is five seconds.
Use the gateway's normal database migration process before enabling enrollment.

## Browser approval

Open `/relay-enroll` from My access, team administration or global
administration, sign in with SSO, and return to that page. Enter the code shown
by the relay **you started on your own computer**. Review the code, device label,
model permissions, lifetime and rate ceilings, then choose an active eligible
team and click **Approve this relay**. The device label is unverified text, not
identity proof. Never approve a code sent by someone else. Requests are
inspected/approved/denied through explicit CSRF-protected POSTs, not by opening
a link; query parameters never automatically approve anything.

**Deny request** explicitly denies the pending request. **Clear this page** or
closing/navigating away clears local page state but does not deny the server
request or revoke an already approved key. Logout immediately clears local
state, even when the server is unavailable; in that case the SSO session may
remain active. Approval already sent may still complete after closing or
logout. Manage issued credentials through My access if revocation is needed.
The page ignores late responses after clearing/logout and prevents concurrent
decisions. It does not use localStorage or sessionStorage.

Approval creates inference-only access within operator ceilings and current
team membership. Return to the local relay to finish. A changed policy or expired request requires starting enrollment again from
the relay; the page does not silently retry approval. Token budget
and team policy continue to apply on inference. Protect the relay's local
journal and use normal revocation controls if that computer is compromised.

Tests cover explicit review/approval, literal rendering of untrusted labels,
CSRF transmission, no URL autoapproval, failed/repeated decisions, unauthenticated
sessions, late-response suppression, and clear/logout/pagehide behavior.
