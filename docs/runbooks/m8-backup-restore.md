# M8 backup, Session maintenance, and isolated restore

## Backup schedules

The installed systemd contracts provide:

- continuous PostgreSQL WAL archiving plus a five-minute archive freshness check;
- differential backup Monday–Saturday at 02:00 local deployment time;
- full backup Sunday at 02:00;
- low-load data export at 03:30;
- cumulative erasure ledger export and encrypted off-host replication every five minutes;
- a Session maintenance opportunity at 04:30.

These `OnCalendar` values deliberately have no `UTC` suffix: systemd evaluates them in
the deployment host's local timezone. The inspected target reported `Asia/Tokyo` on
2026-09-04. Verify and record `timedatectl` for every backup, restore, or timer evidence
run; the installer does not change the host timezone, and account/application timezone
changes do not move these host maintenance windows. Re-review the schedule after any
intentional host timezone change.

All jobs share `/run/lock/telegram-userbot-ops.lock`, have bounded timeouts, and route a
failed unit to a critical journal alert. Configure and test a separate external alert
route before calling alerting production-ready. Off-host S3/restic credentials and target
capabilities are deployment choices; until supplied and tested, production S3
backup/restore remains `NOT RUN`. Synthetic drill results are recorded separately.
See [independent erasure ledger provisioning and recovery](erasure-replica.md).

The Session volume contains exactly one authorized SQLite file named `account.session`.
This is the fixed path consumed by `app`; backup, restore, and restore-gate validation
reject any other name or additional `*.session` file.

The full and differential database timers are persistent, so a missed database window is
run after the host returns. The Session and data-export timers deliberately set
`Persistent=false`: a missed maintenance window is skipped rather than replayed at an
unreviewed time. Start those jobs manually only after re-establishing their maintenance
preconditions. The service also requires the exact environment file, reviewed checkout,
and Docker socket. Its private network namespace exposes only the read-only-bound local
`/run/docker.sock`, removes Docker context/TLS environment overrides, and allows only
Unix-domain connections. The host runner also passes the local socket explicitly, so an
operations job cannot be redirected to a remote daemon by environment configuration.

The Session timer never stops or starts `app`. An operator must first enter maintenance,
stop `app`, and confirm the exact project is stopped. Create one root-only approval:

```bash
project=telegram-userbot-production
release_root="$(readlink -e -- /opt/telegram-userbot/current)" || exit 1
case "$release_root" in
  /opt/telegram-userbot/releases/*) ;;
  *) printf '%s\n' 'current does not resolve inside the release root' >&2; exit 1 ;;
esac
test -d "$release_root" || exit 1
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f "$release_root/deploy/compose.yaml" \
  --project-name "$project" stop --timeout 90 app
test -z "$(docker compose --env-file /etc/telegram-userbot/compose.env \
  -f "$release_root/deploy/compose.yaml" \
  --project-name "$project" ps --status running -q app)"
printf 'SESSION_BACKUP_APPROVED:%s\n' "$project" | \
  install --owner=root --group=root --mode=0400 /dev/stdin \
  /run/telegram-userbot/session-backup-approved
systemctl start tudt-ops@session.service
```

The approval is consumed before the attempt. The host runner also holds the exact
PostgreSQL Session-owner advisory lock and the helper mounts the Session read-only. On
success, explicitly restart `app`, verify Session authorization/account ownership, and
leave maintenance in place until checks pass. On failure, create a new approval only
after diagnosis; never snapshot a live or corrupt Session.

## Restore preflight

Obtain an exact pgBackRest backup set, exact restic snapshot ID, and cumulative erasure
ledger newer than the database restore point with an independently obtained SHA-256.
Verify source signature, image digests, recovery keys, repository access, and the target
host. Choose a previously unused lowercase `tudt-restore-*` project. The runner refuses
existing project containers, networks, or any derived volume and never deletes them.

The current PostgreSQL helper uses `--type=immediate --target-action=promote` with that
exact backup set: it recovers to the first consistent state of the selected backup.
It does not expose arbitrary time/LSN recovery targets or replay all newer archived WAL.
See the [pgBackRest recovery target options](https://pgbackrest.org/command.html#command-restore).

List the exact target before proceeding:

```bash
project=tudt-restore-yyyymmdd-a
docker ps -a --filter "label=com.docker.compose.project=$project"
docker volume ls --filter "label=com.docker.compose.project=$project"
docker network ls --filter "label=com.docker.compose.project=$project"
```

All three outputs must be empty. Resolve the operator symlink to the reviewed checkout
and repeat the project name as the explicit new-target confirmation:

```bash
release_root="$(readlink -e -- /opt/telegram-userbot/current)" || exit 1
case "$release_root" in
  /opt/telegram-userbot/releases/*) ;;
  *) printf '%s\n' 'current does not resolve inside the release root' >&2; exit 1 ;;
esac
test -d "$release_root" || exit 1
sudo /opt/telegram-userbot/venv/bin/python "$release_root/deploy/ops/run_restore.py" \
  --compose-file "$release_root/deploy/compose.yaml" \
  --restore-overlay "$release_root/deploy/compose.restore.yaml" \
  --env-file /etc/telegram-userbot/compose.env \
  --deployment-config /etc/telegram-userbot/config/deployment.json \
  --project-name "$project" --confirm-new-project "$project" \
  --postgres-backup-set '<exact-set>' \
  --session-snapshot-id '<exact-snapshot>' \
  --erasure-ledger /root/recovery/ledger.jsonl \
  --erasure-ledger-sha256 '<independent-sha256>' \
  --evidence-directory /var/lib/telegram-userbot/ops-state
```

The runner executes this exact sequence: validate Compose; restore the exact PostgreSQL
backup; restore the exact Session snapshot; start only PostgreSQL and an empty Redis;
run the forward migration; reset/close the durable gate; stage the independent erasure
ledger; run the gate-open verification;
and verify the final running-service inventory. Gate close intentionally leaves the
database state as `validating`; it is not an approval to read or send. The gate-open
step performs the erasure-ledger replay, database and Session integrity checks,
credential decrypt verification, and side-effect reconciliation, and opens the durable
gate only when every check passes. The final inventory must contain exactly `postgres` and `redis`;
`app`, `control`, `worker`, migration helpers, restore helpers, and the
gateway remain stopped, while `BOOTSTRAP_MAINTENANCE=1` remains set. Opening the gate
does not authorize starting them.

If the final inventory check fails, the runner makes one best-effort attempt to close
the gate again and still returns `RESTORE_RUNTIME_SET_INVALID`. Treat the attempt as
`FAIL` either way. If that close attempt fails or times out, the durable gate state is
unknown: keep maintenance enabled, stop any unexpected service, and manually run and
verify the restore-gate-close step for the same isolated project before any restart.
The terminal evidence record never proves that a failed inventory check left the gate
closed.

Each attempt reserves a new root-owned evidence directory. It writes one immutable,
content-free JSON record per stage, links every record to the previous SHA-256, and writes
`manifest.json` plus `manifest.sha256` last. Preserve both files with the stage records;
a lone terminal marker is not complete restore evidence.

Partial/unknown outbound intents/groups, proactive `send_unknown`, unresolved Copilot or
preview deliveries, due preview deletion, active Control Bot receipt leases, incomplete
erasure, invalid indexes/constraints, credential failure, Session failure, or schema drift
all stop the run. The runner never guesses a send result. Reconcile through reviewed
application procedures under maintenance, then rerun only the gate service for the same
isolated project; do not rerun the new-target orchestrator over existing volumes.

Ledger parsing is versioned; unknown versions/scopes fail closed. The 0034 runtime
supports memory, contact and account entries. `--stage-erasure-replay` commits scope
deletion intents while the gate is `validating`; it preserves request identity and
historical ledger facts, and invalidates progress/inventory from an older backup.
Staging is idempotent within a restore generation. A new `--close` increments the
generation and requires fresh cleanup evidence. Missing target identity or a conflicting
request fails closed; do not delete or rewrite ledger entries to make a restore pass.

Contact/account entries initially stop gate-open with `RESTORE_ERASURE_INCOMPLETE`.
Keep maintenance enabled. Run the bounded [erasure reconciliation and media cleanup
commands](scope-erasure.md#bounded-offline-reconciliation) with the same isolated
Compose project and restore overlays; run export cleanup as needed. These one-shot
commands do not start Telegram or provider clients. Resolve unknown delivery evidence
and unattributed files through the reviewed Operations path. After all requests are
`completed`, rerun the gate-open service for that project. It verifies current-generation
inventory and request identity before any access opens; old ledger timestamps alone
are insufficient. The restored account remains disabled after an account wipe.

If staging itself failed and was repaired, rerun the existing gate service with the
command override `/opt/ops/restore_gate.py --stage-erasure-replay` before cleanup.
Do not rerun the new-target orchestrator over existing volumes. Independent off-host
replication and real backup/restore evidence remain separate deployment work.

## Local synthetic restore contract

Local repositories may test the mechanics without claiming off-host durability. Use only
isolated synthetic pgBackRest/restic repositories, add
`deploy/compose.restore.validation.yaml`, and pass the exact acknowledgement:

```bash
export LOCAL_PGBACKREST_REPOSITORY_DIR=/srv/tudt-validation/pgbackrest
export LOCAL_RESTIC_REPOSITORY_DIR=/srv/tudt-validation/restic
release_root="$(readlink -e -- /opt/telegram-userbot/current)" || exit 1
case "$release_root" in
  /opt/telegram-userbot/releases/*) ;;
  *) printf '%s\n' 'current does not resolve inside the release root' >&2; exit 1 ;;
esac
test -d "$release_root" || exit 1
sudo --preserve-env=LOCAL_PGBACKREST_REPOSITORY_DIR,LOCAL_RESTIC_REPOSITORY_DIR \
  /opt/telegram-userbot/venv/bin/python "$release_root/deploy/ops/run_restore.py" \
  --compose-file "$release_root/deploy/compose.yaml" \
  --restore-overlay "$release_root/deploy/compose.restore.yaml" \
  --validation-overlay "$release_root/deploy/compose.restore.validation.yaml" \
  --ack-local-synthetic-only \
  I_ACKNOWLEDGE_LOCAL_SYNTHETIC_RESTORE_IS_NOT_OFF_HOST_EVIDENCE \
  --env-file /etc/telegram-userbot/compose.env \
  --deployment-config /etc/telegram-userbot/config/deployment.json \
  --project-name "$project" --confirm-new-project "$project" \
  --postgres-backup-set '<exact-synthetic-set>' \
  --session-snapshot-id '<exact-synthetic-snapshot>' \
  --erasure-ledger /root/recovery/synthetic-ledger.jsonl \
  --erasure-ledger-sha256 '<independent-sha256>' \
  --evidence-directory /var/lib/telegram-userbot/ops-state
```

The resulting manifest carries `evidence_scope=local-synthetic-only`. It cannot satisfy
off-host backup, public deployment, RPO, RTO, or 24-hour soak acceptance.

Record actual database, Session, erasure, and whole-host RPO/RTO separately. A successful
script on synthetic local storage is not off-host or production restore evidence. Before
destroying a drill project, inspect its exact project/volume names, preserve content-free
evidence, obtain maintainer approval, and use one shell end-to-end. This runbook does not
authorize automatic deletion.
