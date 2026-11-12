# Member settled usage history

Signed-in members can inspect their own settled gateway usage at
`GET /account/api/usage`. This read-only endpoint requires a browser session.
API keys, including keys with the `account` permission, and relay tokens cannot
use it to enumerate another credential's history. The owner is always taken
from the current validated session; there is no user/team selector.

The default window is the trailing 30 days, sampled from shared database time.
Explicit `since` and `until` values must be timezone-aware ISO8601 timestamps;
the window must be in the past, nonempty, and at most 31 days. The lower bound
is inclusive and the upper bound exclusive. Values are normalized to UTC.
Malformed or out-of-range timestamps fail with HTTP 400. `limit` is 1–100,
default 50. Rows use the indexed user/timestamp/ID ordering, newest first.

```http
GET /account/api/usage?limit=50
```

The response contains:

```json
{
  "since": "2030-05-13T12:00:00+00:00",
  "until": "2030-06-12T12:00:00+00:00",
  "records": [
    {
      "id": "98ff67eb-c92b-4779-a033-8c0b531006eb",
      "created_at": "2030-06-12T10:00:00+00:00",
      "model": "chat",
      "input_tokens": 100,
      "output_tokens": 50,
      "cost_micros": 250,
      "estimated": false
    }
  ],
  "next_cursor": null
}
```

Costs are integer micro-US-dollars: 1,000,000 micros equals USD 1. They are
calculated from configured gateway prices and reported usage, or conservatively
estimated when usage is unavailable. They are not a provider invoice. Estimated
rows may contain zero token counts even though the reservation has a nonzero
estimated charge. Pending reservations and released/rejected requests are not
settled usage rows and do not appear here.

The response explicitly selects safe fields. It contains no prompt, response,
raw credential, credential ID, provider identifier, reservation identifier,
other-user record or team-wide usage. Model aliases are ordinary metadata;
render them as text, never HTML. A member may see their own historical use from
a former team, but not another member's activity in that team.

For another page, send the returned opaque `next_cursor` and optional `limit`.
Do not send `since` or `until` with a cursor. The signed cursor is scoped to the
user and original window and is incompatible with administrative export
cursors. It does not grant access: every request revalidates the browser and
active user under the shared policy gate. A disabled user or revoked browser
cannot continue reading with a previously issued cursor.

Pagination fixes the window and has a deterministic timestamp/ID tie-breaker,
not a frozen database snapshot. Late settlements/backdated rows can change the
history while browsing; refresh from the first page to see newly settled data.
This deliberately returns bounded recent records, not whole-window aggregates
or a completeness-certified audit export. The portal's existing monthly budget
view remains separate. Monthly accounting uses the reservation's admission
period, while this history filters the recorded settlement creation timestamp;
they can differ around month boundaries. Daily request allowances count admitted
requests, including some failures, so they also need not match settled rows.

## Upgrade

Schema revision 16 adds `ix_usage_user_created_id`. Apply the explicit migration
with normal upgrade safeguards and a maintenance window appropriate to the
size of the usage table; index construction is not advertised as online. No
historical accounting rows are rewritten. Rollback requires the documented
verified backup/matching-release procedure.
