# M8 Ubuntu, Docker, and security patch window

Ubuntu 26.04 security updates may install automatically, but automatic reboot is disabled.
Kernel, Docker Engine, Compose plugin, PostgreSQL/pgvector/pgBackRest, and container base
changes require a reviewed maintenance window.

1. Record the exact host, current source commit, image digests, OS/kernel, Docker/Compose,
   pending packages, disk/clock, restart counts, unknown sends, and backup ages.
2. Stop if a verified PostgreSQL or Session backup cannot be completed, the restore gate
   is closed, side effects need reconciliation, disk is critical, or the candidate was not
   exercised first on an isolated Ubuntu 26.04 amd64 host.
3. Enter maintenance, drain business work, stop `app` cleanly, and take the owner-gated
   Session backup. Do not combine a database migration with an unrelated emergency host
   patch unless the reviewed change explicitly requires both.
4. Install only the approved package/image set. Do not change Docker major or Compose
   behavior directly on the target without staging evidence.
5. If a reboot is required, keep public routing/AUTO blocked. After reboot verify time,
   encrypted mounts, firewall, Docker, project identity, volume inventory, PostgreSQL/WAL,
   Redis rebuild, schema, Session owner, queues, reconciliation, internal health, backup
   timers, and the independent alert route.
6. Restore the gateway and business processes only after post-patch checks pass. Record a
   content-free result and any reboot/rollback decision.

Package rollback follows the vendor-supported package procedure only if data formats stay
compatible. Database-image rollback follows `m8-upgrade-rollback.md`; data rollback uses a
new-volume restore, never an in-place downgrade. If compatibility or recovery evidence is
missing, record `BLOCKED` and keep maintenance enabled.
