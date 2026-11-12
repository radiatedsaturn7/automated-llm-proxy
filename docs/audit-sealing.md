# Offline authentication of completed audit exports

`python -m gateway.audit_seal` optionally seals a **completed** collector window.
It does not change the collector, resume cursors, API, retention or export format,
and makes no network calls. Partial exports must finish through the normal
collector before sealing. Both seal and verify acquire the existing collector
lock without changing its contents; verification never truncates an uncommitted
tail. A tail is an error, not something this tool repairs.

## Dedicated key and external custody

Use a dedicated cryptographically random **32-byte binary key file**, not any
installation encryption, signing or API key. Provision it through your approved
secret-management process. Do not put the bytes in command arguments, shell
history, repository files or exported artifacts. The tool reads its path from
`AUDIT_SEAL_KEY_FILE`. Key and anchor must both be outside the export directory,
in existing owner-only mode 0700 directories. All input files must be owned by
the executing user, regular single-link mode 0600 files. Symlinks in input paths
are rejected. This is an offline operator tool, not a Kubernetes projected-secret
loader.

Example paths below refer to operator-provisioned files, not generated keys:

```sh
export AUDIT_SEAL_KEY_FILE=/secure/audit-key/key.bin
python -m gateway.audit_seal seal \
  --output-dir /exports/completed-window \
  --anchor /secure/audit-anchors/window-2026-01-01.json
python -m gateway.audit_seal verify \
  --output-dir /exports/completed-window \
  --anchor /secure/audit-anchors/window-2026-01-01.json
```

An existing anchor is never replaced. Choose a new anchor deliberately for a new
snapshot; do not replace a failed verification with a fresh seal. Successful
output contains only status, byte count and record count. Failures use a static
diagnostic and exit 1, without records, credentials or key bytes.

## What is authenticated

The v1 manifest contains a fixed format and filename, normalized gateway/window
specification, exact byte length, record count and SHA256 of the **exact ordered
JSONL bytes**. Its HMAC-SHA256 covers deterministic sorted compact ASCII JSON of
that snapshot, prefixed by the domain `thunderbolt/audit-seal/v1` and a NUL byte.
This stable protocol domain is not a product display name. Verification first
authenticates the manifest, then compares the streamed snapshot. Changing,
removing, reordering, appending or truncating records fails against the trusted
anchor, even if someone updates the export's unkeyed checkpoint hash.

Sealing validates the complete collector checkpoint, byte hash, inode/device
binding, window and bounded metadata records. Verification performs the same
checks. Consequently this initial tool verifies the original local collector
output, not a copied archive whose inode/device checkpoint binding has changed.
Scans are bounded to the collector's 256 MiB cap, 8 KiB per record, 16 KiB per
checkpoint/manifest and constant-size streaming buffers. It holds the collector
lock throughout the scan/publication and detects ordinary concurrent changes.
An uncooperative attacker with the same process privileges is outside the
filesystem isolation guarantee.

The anchor is written via a private temporary file, fsynced, published without
replacement and its directory fsynced. A crash before publication leaves no valid
anchor. A crash during publication may leave a two-link temporary/anchor pair;
strict verification rejects it rather than trusting an ambiguous artifact.
Preserve evidence and recover through an operator-reviewed procedure. A command
failure after publication does not imply the anchor was absent; inspect and
verify the existing anchor rather than blindly resealing.

## Limits and trust

Keep the key confidential and the correct anchor under separate trusted custody,
with access controls and backups appropriate to the environment. A different
folder on the same machine is a path separation check, **not** a separate security
boundary. The HMAC key is shared by signing and verification: anyone holding it
can forge a seal. This is neither public-key nonrepudiation nor WORM storage.

A seal authenticates the snapshot trusted at sealing time. It does not prove
historical completeness, detect records omitted or changed before collection,
or establish that the gateway supplied the whole requested window. Replacing
both export and anchor with an older valid pair is not detected without an
independent trusted latest-anchor inventory. Protect that inventory separately.
Key rotation and anchor migration are explicit operator procedures, not automatic
behaviors implemented by this command. No cloud archive, external transmission,
retention deletion, scheduled sealing or legal compliance certification is added.

Synthetic local tests cover exact-byte mutations with forged checkpoints,
wrong keys, modified anchors, incomplete windows/tails, private file protections,
non-overwrite and interrupted writes. These tests do not establish an operational
recovery SLA or resistance to a compromised host/key custodian.
