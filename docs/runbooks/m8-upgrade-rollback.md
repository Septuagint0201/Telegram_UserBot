# M8 upgrade and rollback

## Upgrade gate

Use one approved signed commit and immutable application/database/gateway/operations
digests. Stop if disk is at 90% or free space is below 1 GiB, clock is unsynchronized,
the restore gate is not open, any send is partial/unknown, jobs or outbox are stale, or
the latest verified PostgreSQL and Session backups are older than one hour.

1. Put the current deployment in maintenance; prohibit new models and sends and wait for
   leases/RPCs to reach a safe boundary.
2. Run a PostgreSQL backup. For the Session backup, stop `app`, verify it is stopped, use
   the explicit one-use approval described in `m8-backup-restore.md`, and only restart
   `app` after the owner-locked snapshot succeeds.
3. Verify the target commit signature, Disclosure, dependency locks, image digests/SBOM,
   deployment manifest, and `docker compose ... config --quiet` without printing config.
4. Pull only exact digests. Start PostgreSQL/Redis, run the one-shot migration, then run
   role closure and schema compatibility. A failed or contended migration stops here.
5. Start app/control/worker under maintenance. Verify readiness details, Session/account
   owner, durable queue inventory, provider configuration without live provider calls,
   reconciliation, disk, backup age, and internal monitor output.
6. Start the gateway only after the key-only route/TLS boundary is ready. Clearing
   maintenance and enabling AUTO are separate maintainer actions outside this procedure.

Observe the candidate for at least 30 minutes after any authorized activation and retain
content-free before/after evidence.

## Application rollback

Application rollback is allowed only when the previous image declares the current schema
inside its compatibility window and its exact digest has passed the same preflight. Keep
maintenance enabled, stop business processes, switch the manifest back to the reviewed
digest, start internal services, and repeat reconciliation/readiness checks.

If the previous binary cannot read the new schema, status is `BLOCKED`; do not attempt to
force it or run an Alembic downgrade.

## Database rollback

Database rollback is not an automated Alembic downgrade. It is an isolated pgBackRest
restore/PITR decision that explicitly accepts loss after the selected point and must use
the latest erasure ledger overlay. Follow `m8-backup-restore.md` with a new
`tudt-restore-*` project and new volumes. Never restore over the current production
volume. Promote only after the independent restore acceptance and maintainer approval.
