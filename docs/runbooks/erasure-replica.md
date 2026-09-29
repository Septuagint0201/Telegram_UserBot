# Independent erasure ledger repository

The five-minute `erasure-ledger` job exports the cumulative ledger and copies it to a
dedicated, encrypted restic repository. A local JSONL export alone is not backup success.
The job reads the exact uploaded snapshot back, compares every byte, and only then writes
`ledger.receipt.json` and `/ops-state/erasure-replica.json`. The monitor raises critical
`ERASURE_REPLICA_STALE` when that marker is missing, malformed, or older than 15 minutes.
Its age measures the database export time; retrying an old upload cannot make it fresh.

## Provisioning

Set `ERASURE_RESTIC_REPOSITORY=s3:https://<endpoint>/<dedicated-bucket>/<deployment>` in
the reviewed host Compose environment. The endpoint must use HTTPS with no credentials,
query string, or fragment embedded in the URL. Use a separate repository and credentials
from both PostgreSQL and Session backups. Add these secret files according to
`deploy/config/secret-files.json` (`root:21018`, mode `0440`):

- `erasure_restic_password`: 32–256 ASCII bytes, no trailing newline;
- `erasure_s3_access_key` and `erasure_s3_secret_key`: 16–256 ASCII bytes each.

The export service receives only the existing read-only export database identity,
erasure HMAC key, and these repository secrets. Repository passwords and S3 keys enter
only the restic child environment. They are never Compose environment values or journal
output. Keep recovery keys outside the source host and outside these encrypted backups.

Resolve `release_root` and `project` as in the main backup runbook. Initialize a new,
reviewed repository explicitly, once:

```bash
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f "$release_root/deploy/compose.yaml" --project-name "$project" --profile ops \
  run --rm --no-deps --pull never erasure-ledger-export \
  /opt/ops/erasure_replica.py --init-repository
systemctl start tudt-ops@erasure-ledger.service
```

A scheduled job never initializes or recreates a repository after an access failure.
It authenticates the prior remote ledger and requires every earlier request to remain
unchanged in the next export, including when the local export has been lost. A filesystem
lock serializes manual and scheduled copies on the source host; operate one writer per
repository. Do not run independent writers with separate local lock directories.

No automatic `forget` or `prune` runs for the ledger. Configure and exercise off-host
retention/versioning and a separate external alert route before production acceptance.
Ordinary restic credentials do not themselves provide storage-enforced append-only
protection. Backend loss/rollback must also be detected against independently retained
receipts; listing a repository's newest snapshot cannot prove it is globally current.

## Recover before opening the database gate

Retain each successful receipt independently of the source machine. It contains only
deployment ID, exported time, full restic snapshot ID, and JSONL SHA-256. Select a receipt
that covers all completed erasures through the recovery cutoff; a matching digest proves
integrity of that chosen ledger, not freshness relative to later unseen erasures.

In the reviewed recovery sandbox, mount the ledger repository secrets and deployment
configuration, then recover to an **absent** file under the private ledger directory:

```bash
/opt/venv/bin/python /opt/ops/erasure_replica.py --recover \
  --snapshot '<64-hex-restic-snapshot-id-from-independent-receipt>' \
  --sha256 '<64-hex-jsonl-digest-from-independent-receipt>' \
  --ledger /erasure-ledger/recovered.jsonl
```

The command rejects `latest`, abbreviated snapshot IDs, an existing destination, wrong
hashes, malformed ledgers, and a different deployment/account. It opens no database gate.
Pass the recovered file and **the independent receipt digest** to the existing
[`run_restore.py` workflow](m8-backup-restore.md), then complete the
[scope deletion replay](scope-erasure.md) before verification and gate opening.
Keep application, control and normal worker processes stopped during replay.

## Synthetic validation boundary

Local repositories are allowed only at `/local-erasure-repository` with
`TUDT_LOCAL_BACKUP_VALIDATION=explicit-local-synthetic-only`. This exception is for isolated
tests. It does not establish S3 availability, durable retention, production RPO/RTO,
Telegram Session authorization, or an external alert delivery guarantee.

The 2026-09-28 drill and its observed results are recorded in
[the takeover audit](../audits/2026-09-20-takeover.md). The drill transfers encrypted
PostgreSQL, Session and ledger repositories through a second host, with separately held
synthetic keys. Media cache files used to challenge deletion replay are synthetic fixtures.
