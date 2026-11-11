# Operations

## Transient-record cleanup

Run `python -m gateway.maintenance` to preview up to 1,000 expired OIDC states, expired browser sessions, and rate buckets older than one day per table. `--apply` performs the cleanup and writes a metadata audit event; `--batch-size` accepts 1–10,000. Run repeatedly until zero records are eligible. The job uses the same database policy lock as live traffic; keep batches modest and schedule during low traffic. Run from a short-lived task with the same configured database and secrets as the gateway, never from every app replica.

This command **never deletes keys, audit history, usage, or reservations**. In particular, unresolved reservations are not released by time: an interrupted upstream call may still have incurred charges. Release them through the explicitly acknowledged administrator reconciliation endpoint only after provider billing verification. Archival/export of audit and billing data, retention obligations and database backup policy remain deployment responsibilities.

## Recovery

Store database backups together with the matching encryption/signing key versions in protected secret management. Losing the master key loses access to encrypted upstream credentials; changing signing keys invalidates key digests and sessions, not just access tokens. Rotation of those root secrets requires a planned migration, not an environment-variable flip. Practice restores before production rollout.
