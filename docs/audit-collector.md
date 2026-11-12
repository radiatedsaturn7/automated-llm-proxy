# Operator audit collector for customer-controlled ingestion

Run explicitly; this CLI neither schedules work nor pushes to any SIEM/vendor.
It reads the existing administrative signed-cursor audit endpoint with an
operator-provided credential. Application replicas remain stateless. The local
collector intentionally maintains a checkpoint, separate from the gateway.

```sh
mkdir -m 700 /absolute/private/audit-window
# Supply AUDIT_GATEWAY_KEY using your secret manager, not command arguments.
python -m gateway.audit_collect --gateway https://gateway.example.com \
  --since 2026-11-01T00:00:00Z --until 2026-11-02T00:00:00Z \
  --output-dir /absolute/private/audit-window --page-size 100 --max-pages 10
```

Use explicit timezone-aware past windows of at most 31 days. The credential
must authorize the existing administrative export endpoint; the CLI cannot
expand its permissions. HTTPS is required except loopback development. Redirects
and ambient proxy settings are disabled. Each page has a 30-second wall limit
and 4MiB response ceiling. Requests require identity encoding; compressed
responses are rejected before reading to prevent decompression amplification. Page size and maximum page count are independently
bounded 1–1000; output per directory is capped at 256MiB. Use smaller windows
when necessary. No inference/provider calls occur.

Only id, actor_id, action, target_id and created_at are emitted as JSONL.
Free-form details and unexpected fields are discarded, deliberately excluding
future plugin payloads/prompts/credentials. UTC is explicit; timezone-less SQLite
server timestamps are interpreted as UTC, matching gateway storage conventions.
Actor/target identifiers are still security/personal metadata: protect the
output and authorize any subsequent SIEM sharing separately. This is not a
complete free-form audit payload export.

## Durable files and crash behavior

The existing directory must be owned by the current user, mode0700, with no
symlinks in its absolute path. Fixed audit.jsonl, checkpoint.json and lock files
must be owned, regular, single-link mode0600 files. A process lock prevents two
collectors using the directory simultaneously. POSIX is required.

For every page, the collector appends JSONL, fsyncs the output, then atomically
replaces a checkpoint and fsyncs the directory. The checkpoint binds gateway,
window, file inode/device, committed byte offset, prefix SHA256 and cursor. On
resume, committed bytes are verified; truncation/replacement/modification fails
closed. Bytes beyond the last committed offset are an uncommitted page, possibly
a partial JSON line: they are truncated before fetching again. Thus a crash
cannot append new lines after corrupt trailing JSON. Only the uncommitted tail
is repaired; committed data is never silently discarded. Do not manually edit,
rename, rotate or truncate collector files. A missing checkpoint with nonempty
output is rejected.

Repeat the identical command to resume a bounded run. Exit0 means that window
completed (a completed checkpoint causes no further requests); exit3 means the
page bound was reached and the directory can be resumed; exit2 means failure.
Diagnostics omit credentials, raw response bodies and cursors. An unexpected
write failure requires reopening the directory before retrying.

A downstream live tailer may have consumed a page before a crash causes its
local replay. Delivery is **at least once**, not exactly once: deduplicate by
audit record id. If copying a completed file to a SIEM, retain the checkpoint
and handle ingestion acknowledgements yourself. Files are not immutable, WORM,
signed audit attestations, or a tamper-proof compliance archive.

## Cursor and window limitations

The current gateway cursor has no time-to-live. It is bound to the actor user,
export kind and fixed window, and signed with the gateway signing key. Changing
the actor or signing key may invalidate it. The collector stops on rejection;
it never silently resets to an earlier position. A different key for the same
authorized actor can continue if the signing key is unchanged. To replay an
invalidated window, deliberately choose a **new private directory** and the same
window, then deduplicate downstream. Retention/deletion can remove old records;
no exporter can reconstruct them. The API is not an MVCC snapshot: transactions
that commit late with earlier timestamps may require deliberate overlapping
window replays. A completed local checkpoint does not poll for late arrivals.

Omit --output-dir to emit JSONL on stdout. This mode has **no checkpoint**:
flushing a pipe does not prove the downstream sink persisted anything. Bounded
incomplete stdout runs must be replayed from the window start; deduplicate by id.
Use durable directory mode for resumable collection.

## Optional offline authentication

After collection completes, use the [audit sealing tool](audit-sealing.md) to create an authenticated snapshot with a dedicated external key and anchor. It leaves collection and resume behavior unchanged and makes no network calls. Verification is read-only; failed verification must not be treated as permission to overwrite the anchor.
