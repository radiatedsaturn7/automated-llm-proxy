# Maintenance-window master-key rotation

Brrrtgate supports an explicit, transactional rotation of **database encryption**
keys. It does not rotate `GATEWAY_SIGNING_KEY`, promise zero downtime, or fence old
binaries/direct SQL writers. Use a maintenance window and remove those writers first.

## Configuration and initialization

`GATEWAY_MASTER_KEY` is the sole encryption primary. Optional
`GATEWAY_MASTER_DECRYPT_KEYS` is a JSON array of at most three distinct additional
Fernet keys, used only for decryption. Duplicate decoded key material, including the
primary, is rejected. Its `_FILE` selector uses the same trusted mount policy as other
[installation secrets](secret-files.md). No key is accepted on the CLI command line.

Migration v13 verifies every non-null provider credential and enrolled/pending MFA seed
using the **primary alone**, then records a domain-separated fingerprint of its decoded
key material. Wrong-key initialization fails even if a fallback could decrypt the data.
Fresh empty databases initialize through the same migration command. Re-running migration
never resets the fingerprint. Apply v13 with the existing primary before planning rotation.

Supported application transactions check the fingerprint under the policy gate. A
replica with an old primary fails readiness and policy operations with 503 after rotation.
Fallbacks do not bypass that fence. This does not cancel already admitted work, prevent
old application versions from writing, or protect against direct database changes.

## Required operator sequence

1. Back up the database and securely escrow its matching master and signing keys.
   Verify an isolated restore with a compatible release. Keep historical keys for all
   retained backups. Never place keys in Git, tickets, arguments, or reports.
2. Drain traffic and in-flight requests, suspend jobs, and stop all database writers.
   Remove older binaries. Removing ingress alone is insufficient. Deploy the current
   version and migrate v13 with the original primary before this rotation procedure.
3. Prepare immutable/versioned secrets for the target configuration: new primary plus
   the previous key in the decrypt-only list. Keep the signing key unchanged. Multiple
   mounted files are not read as one atomic generation; do not mutate them mid-startup.
4. Obtain the current fingerprint with `python -m gateway.rotate_master --inspect`.
   This bounded read-only operation verifies the schema and reports `database_fingerprint`,
   `configured_fingerprint`, and `primary_matches`, even when the configured primary
   differs. It does **not** decrypt or verify ciphertext; `ok: true` only means inspection
   succeeded, not that the primary matches. It cannot be combined with `--execute` or
   `--expected-current-fingerprint`. A fingerprint is not a key, but must correspond to
   the intended installation. Configure a one-off operator process
   with the target primary and old decrypt key, then run the dry run:

   ```sh
   python -m gateway.rotate_master --expected-current-fingerprint "$CURRENT_FINGERPRINT"
   ```

5. Review the result. The command verifies schema, expected fingerprint, and all stored
   encrypted fields. Defaults cap processing at 100,000 rows and 300 seconds; use
   `--max-rows` (1..1,000,000) and `--timeout-seconds` (1..3600) to set reviewed bounds.
   Lock waits and database statement limits can also terminate the operation. No network
   provider requests occur. Execution requires repeating the command with `--execute`.
6. Execution holds the exclusive database policy gate and changes all relevant ciphertext
   and the fingerprint in **one transaction**. A bad ciphertext, invalid MFA seed, row
   bound, or cancellation aborts the transaction. Success emits only counts, execution
   status and the target fingerprint. Already-primary ciphertext is verified without
   being rewritten; repeat execution with its current fingerprint is safe.
7. Restart every replica/job with the target configuration; run `python -m gateway.check`
   and verify readiness and representative workflows before restoring traffic. Confirm
   the primary on every deployment. Keep old key material in protected backup escrow.

An interrupted command can have an uncertain commit outcome (for example, a connection
loss during commit). Run `python -m gateway.rotate_master --inspect` to observe the stored fingerprint, and rerun a dry run with the observed
expected fingerprint; do not assume failure output proves rollback after a lost commit
acknowledgment. No automatic retry is performed. Re-running execution with the same target
primary verifies and converges remaining old ciphertext without changing logical secrets.

The CLI's atomic gate can block application work, which is another reason to drain first.
This is not a background or scheduled rotation service, and it does not orchestrate your
replicas, secrets manager, ingress, backups, or incident response.

## Retirement, rollback and backups

After all writers are demonstrably using the new primary, run a full dry run with **no
fallbacks** to prove current stored ciphertext is decryptable by that primary alone.
Check every replica before removing runtime fallbacks. This verifies current data, not
historical backups. Retain protected old keys while those backups remain in retention.

Rollback is explicit reverse rotation during another drained maintenance window: configure
the old key as primary and the new one as fallback, supply the current new fingerprint,
perform dry run, then execute. Restart all replicas with the restored primary. Merely
changing an environment variable back fails the fence and does not rewrite ciphertext.
Schema downgrade is unsupported; restore a verified backup with its matching application
release and secrets when a full rollback is required.

## Signing keys and compromise

`GATEWAY_SIGNING_KEY` also participates in stored credential/session-related digests as
well as JWT and cursor signing. Replacing it can invalidate authentication and make
existing credentials unrecognizable. This tooling deliberately does not change it and
provides no signing-key overlap migration.

Suspected exposure requires incident containment, evidence preservation and review of
accessible snapshots. Re-encryption does not undo prior disclosure: provider credentials
may need revocation/replacement at their issuing services, and exposed MFA seeds may
require secure factor re-enrollment. Coordinate those actions separately. This tooling
is not a substitute for incident response or an external security assessment.
