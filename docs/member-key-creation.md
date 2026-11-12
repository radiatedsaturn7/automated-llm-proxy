# Policy-controlled member credentials

Browser members can create their own API credentials only after an administrator
enables a per-user issuance template. This is disabled by default. It does not
grant a new account role, provider access, free usage, or additional spend budget.

Administrators GET/PUT `/admin/api/users/{user_id}/key-policy`. The template has:
`enabled`, explicit `team_id` (null means a teamless user), nonempty
`allowed_models` (literal aliases, no wildcard), `permissions` (inference or
inference plus account), `expires_in_days` (1–90, default 30), `rpm` (default 60),
`tpm` (default 100000), `max_active_keys` (1–10, default 3), and
`max_total_keys` (1–1000, default 100). Active cannot exceed total.
The selected team must be an active membership. Changes revoke ALL existing
self-service credentials for that user, including rotated descendants.
Identical policy writes do not revoke. Other administrator-issued keys are not
revoked. PUT returns `changed` and `existing_self_service_keys_revoked`.

Members GET `/account/api/key-policy` for the template plus `active_keys`,
`total_keys`, `can_create`, and `disabled_reason`. POST
`/account/api/keys` accepts only `name` and an `operation_id` UUID.
A browser session and CSRF header are required; bearer keys cannot create keys.
Response: `{credential, key, operation_id, replayed}`. Only the first successful
response contains the plaintext `key`; it is never stored recoverably.
A retry with the same UUID/name returns the original credential receipt and
`key:null,replayed:true`. The receipt remains on the original version after
revocation, expiry, or rotation. It cannot create or revive a credential.
If that first response was lost, revoke the receipt's credential and explicitly
issue a new operation. A UUID reused by another user/name is rejected.

All self-service keys and their descendants share ONE user-wide minute RPM/TPM
pool, even across revoke/recreate and admin/member/team rotation. Changing a
policy does not erase the pool; lower limits account for already reserved work.
The ordinary shared daily request allowance, immutable usage records, and
user/team financial budgets and in-flight reservations remain unchanged.
Administrator-issued credentials retain their existing independent rate limits.

The lifetime version cap counts revoked, expired, and rotated self-service
credentials. Rotation at the cap fails atomically without revoking the current
key. Administrators may increase the cap only up to 1000; this release does not
automatically delete/archive key history. This deliberately bounds stored
credential versions and prevents repeated creation/rotation from evading limits.

Schema revision 17 preserves pre-existing keys as non-self-service, adds a unique
nullable operation receipt index, and stores one template per configured user.
