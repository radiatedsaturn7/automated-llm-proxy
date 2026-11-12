# Optional usage allowances and self-service resets

This is a gateway-owned product allowance, **not a provider entitlement or free
API usage**. Upstream calls still cost money. Real spend, pending reservations,
user/team monthly hard budgets, per-key RPM/TPM limits, model scope and credential
expiry never reset or increase when a member redeems a reset.

The feature is disabled for all users by default. An administrator can configure
an existing user with:

```http
PUT /admin/api/users/USER_ID/usage-allowance
Content-Type: application/json

{"daily_request_limit":100,"monthly_reset_limit":4}
```

Use an authorized administrator credential or an administrator browser session
with its CSRF header. `daily_request_limit` is 1–100,000,000 or `null` (disabled).
`monthly_reset_limit` is 0–4, default 0. The PUT replaces these two settings;
omitting `daily_request_limit` disables the allowance. GET at the same path
returns the configuration plus current allowance and reset counters. Ordinary
members and team administrators cannot grant themselves resets or raise limits.

All inference credentials belonging to one user share the daily allowance,
including API keys and local relay credentials, across teams and replicas. A
successfully admitted request consumes one request; upstream failures and
client cancellations do not refund it. Rejected admission consumes none. This
is a request count, not a token count or dollar balance. Existing per-minute
request/token controls still apply. A new key or rotation cannot evade it.

The allowance renews at 00:00 UTC every day. Reset credits renew on the first
UTC day of each calendar month. Unused credits do not roll over. Limits and
periods use database time after admission/reset serialization, not replica time.
A reset is available only after the daily request allowance is exhausted. It
restores the user's current daily allowance without changing financial
history. Administrators changing, disabling or re-enabling the configuration do
not replenish already-consumed reset credits or clear today's allowance count.

## Member API

A signed-in browser can GET `/account/api/usage-allowance` for:

- `enabled`, `day`, `daily_request_limit`, `requests_used`, `requests_remaining`
- `month`, `monthly_reset_limit`, `resets_used`, `resets_remaining`
- `allowance_renews_at`, `resets_renew_at` (UTC timestamps)

`requests_used` counts requests since the most recent daily renewal or reset,
not total billable activity. Use normal usage records for real accounting.

To redeem a credit, POST `/account/api/reset-usage` with the session's
`X-CSRF-Token` and JSON:

```json
{"operation_id":"7f1c4480-6072-44c7-90ed-f773b4ed3bc7","expected_day":"2030-01-30"}
```

Generate a new UUID for each intended reset, and copy `expected_day` from the
latest status. **Reuse that same UUID/day on an uncertain-response retry**;
do not generate a fresh operation until the prior result has been resolved.
Successful retries in the same month return `replayed:true` without consuming
another credit or erasing subsequent requests. Previous-month requests are
rejected by their stale day rather than consuming a new month's credit. The
server retains at most four successful operation IDs per user/current month;
normal audit records retain the reset history.

Bearer API/relay credentials cannot redeem credits or view this account-wide
allowance endpoint. A member cannot target another user. A missing/stale session
or CSRF token is rejected. Resets with no credits, disabled policy, a not-yet-exhausted
allowance, or a stale day are refused without consuming a credit. A stale
day requires refreshing status; it must not be silently changed on a retry.

The returned `reset_applied:true` means a reset was applied for that operation,
not that the next inference is guaranteed: hard spend/rate controls, provider
availability and authorization still apply. Every successful reset and every
administrator configuration change is audited. No raw credential is logged.

## Deployment

Schema revision 15 adds one bounded allowance row per configured user. Run the
explicit migration with replicas drained/stopped using the documented upgrade
procedure, then start the matching application version. Older binaries must not
serve this schema. No historical Usage or Reservation rows are changed.

## Web interface

Administrators select **Users → Usage allowance** to configure a person's daily
request allowance and monthly reset-credit limit. Leave the daily limit blank to
disable it. The UI warns that provider usage is not free and policy changes do
not erase consumed requests or credits.

Members see today's allowance, remaining reset credits, and UTC renewal times
at `/portal`. **Use one reset credit** becomes available only when the allowance
is exhausted and a credit remains. Confirmation explains that financial and rate
limits stay enforced. If a network/server response is uncertain, **Retry pending
reset** reuses the same operation ID and expected day. Do not start a new reset
in another tab while resolving an uncertain operation. Credentials and operation
state are not persisted to browser storage; navigating away clears local state.
