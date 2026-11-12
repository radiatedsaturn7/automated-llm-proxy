# Encryption-key operations and recovery

## Current support and limits

Brrrtgate currently accepts **one** `GATEWAY_MASTER_KEY`, a Fernet key. It encrypts
provider credentials and local MFA seeds, including pending enrollment seeds.
There is no supported keyring, online key rotation, or bulk re-encryption command.
Changing this setting does not migrate stored ciphertext. Existing credentials and
MFA factors can become unreadable, even when the server process starts successfully.

Mounted secret files change how key material is delivered, not its lifecycle. Keep
the same bytes when moving from environment variables to mounted files. Use an
immutable/versioned secret per rollout; different files read during startup are not
necessarily from one atomic secret generation. See [mounted secrets](secret-files.md).

Do not replace the master key in a live deployment, run a blind SQL update, or remove
an old key merely because a new deployment appears healthy. A stale replica can still
write ciphertext using its old key. This release has no database encryption-generation
fence that prevents that behavior.

## Backup escrow

Protect the database backup and its corresponding master key as a recoverable pair.
Store key material separately from the database backup, under restricted, audited
access with a tested recovery procedure. Preserve keys required by retained historical
backups. Deleting a key can make otherwise valid backups unusable.

Also retain the installation's signing key and the application version/schema context.
Do not put installation secrets in Git, tickets, command arguments, exported reports,
or unprotected backup manifests. Metadata identifying a protected key version is useful;
the key bytes themselves are not ordinary backup metadata.

Test restores in an isolated environment with no outbound provider access or public
traffic. Use matching protected secrets and a compatible application/schema version.
Run the existing local preflight:

```sh
python -m gateway.check
```

This checks configuration, schema, provider credential decryption, and MFA seed
readability. It does not verify upstream credentials, IdP access, every operational
workflow, or that other replicas have the same key. It is not a rotation command.

## Planning a change

A master-key change requires a separately reviewed and tested migration procedure;
this release does not supply one. Budget for a maintenance outage rather than assuming
rolling deployment is safe. Before approving such a procedure:

1. Inventory all gateway replicas, maintenance jobs, administrative clients, and other
   database writers, plus their application and secret versions.
2. Verify an isolated restore of a recent backup with the current key. Preserve that
   backup and its protected key material through the change and rollback period.
3. Define how traffic will be drained, in-flight work allowed to finish, jobs suspended,
   and all writers stopped. Removing public ingress alone does not stop writers.
4. Require a tested migration covering provider credentials and **both** enrolled and
   pending MFA seeds, transactional failure behavior, and recovery after interruption.
5. Define verification of every replica before traffic reopens, a check for ciphertext
   still requiring the previous key, and an explicit rollback procedure. Changing an
   environment variable back is not sufficient once ciphertext has been rewritten.

Until that procedure exists, keep the current key stable. For an existing deployment,
contact the maintainer/operator responsible for migration design rather than attempting
an undocumented conversion. A fresh empty installation can use a newly generated key;
it cannot thereby read data from the previous installation.

## Signing keys are different

`GATEWAY_SIGNING_KEY` is not the master encryption key. Besides JWT and cursor signing,
it participates in stored credential/session-related digests. Replacing it can invalidate
sessions and cursors and make existing API or agent credentials unrecognizable. This
release does not provide a signing-key rotation/overlap migration either. Do not rotate
it as a substitute for master-key rotation, or expect retaining the master key alone
to restore authentication continuity.

## Suspected compromise

Treat suspected key exposure as an incident. Contain access, preserve evidence, identify
which database snapshots and encrypted secrets were accessible, and involve the security
owner. Re-encrypting data would not undo exposure of an earlier snapshot. Provider
credentials may require revocation/replacement at their issuing services; exposed MFA
seeds may require factor reset and secure re-enrollment. Installation signing-key exposure
has separate authentication consequences. Scope those actions explicitly and plan for
interruption; do not publish secrets while coordinating recovery.

## Future design, not available functionality

A potential future implementation would combine a bounded decrypt-only fallback keyring,
a primary-encryption-key fingerprint stored in the database, admission/write fencing for
stale supported replicas, and an atomic, dry-run-first re-encryption tool. Even that design
would require removing older binaries and controlling out-of-band database writers.
None of these rotation mechanisms is implemented or promised by this document.
