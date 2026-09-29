# M8 deployment operator boundary

The current deployment schema head is `0036_worker_complete`. The example
deployment configuration remains non-deployable: production images are `NOT_BUILT`, and
the production SBOM/license inventory is `NOT_GENERATED`. These artifacts and the
corresponding Ubuntu 26.04 runtime evidence must be produced and reviewed before M8 can
be closed.

`compose.yaml` is the production definition. Its `BOOTSTRAP_MAINTENANCE` default is `1`, so
an incompletely composed runtime remains not-ready. The host operator must use the
canonical `/opt/telegram-userbot/venv` created from reviewed Ubuntu CPython `3.14` and the
repository's hash-locked bootstrap/runtime requirements. Run from the reviewed checkout,
render the exact Compose configuration, and run the content-free preflight before any
`up`:

```bash
release_root="$(readlink -e -- /opt/telegram-userbot/current)" || exit 1
case "$release_root" in
  /opt/telegram-userbot/releases/*) ;;
  *) printf '%s\n' 'current does not resolve inside the release root' >&2; exit 1 ;;
esac
test -d "$release_root" || exit 1
cd -- "$release_root"
test -x /usr/bin/python3.14
/usr/bin/python3.14 --version
sudo /usr/bin/python3.14 -m venv /opt/telegram-userbot/venv
sudo /opt/telegram-userbot/venv/bin/python -m pip install \
  --require-hashes -r "$release_root/requirements/bootstrap.lock"
sudo /opt/telegram-userbot/venv/bin/python -m pip install \
  --require-hashes -r "$release_root/requirements/runtime.lock"
set -Eeuo pipefail
umask 077
compose_config="$(mktemp /tmp/telegram-userbot-compose.XXXXXX.json)"
trap 'rm -f -- "$compose_config"' EXIT
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f "$release_root/deploy/compose.yaml" config --format json > "$compose_config"
SECRET_ROOT=/etc/telegram-userbot/secrets \
PYTHONPATH="$release_root/src" \
  /opt/telegram-userbot/venv/bin/python -m telegram_userbot.processes.deployment_preflight \
  --deployment-config /etc/telegram-userbot/config/deployment.json \
  --compose-config "$compose_config" \
  --source-root "$release_root"
```

If `/usr/bin/python3.14` is not present, install the reviewed Ubuntu `python3.14`
runtime package before continuing. Do not substitute an unreviewed `python3` alias or
run the preflight from an unreviewed source tree. The source path is supplied explicitly
because this host-only checker runs from the reviewed checkout rather than an editable
system install. Reuse the venv for later host-side checks and reinstall the exact locks
when the reviewed source changes. After the preflight, pull the exact image digests
recorded in the deployment manifest once; all subsequent `up`/`run` commands must use
`--pull never` so a missing image fails closed instead of resolving a tag.

Preflight requires `/etc/telegram-userbot/secrets` to be a canonical, non-symlink,
`root:root 0700` directory. Every source file must match its manifest `root:<reader-gid>
0440` contract, every rendered Compose secret must resolve back to that exact source root,
and every rendered service image must equal the immutable reference in the deployment
manifest. Output contains only stable result codes and counts. Run it on the host; no
business container receives the Docker socket.

`/etc/telegram-userbot/config` is non-secret but not world-readable: it and every
container-read config file use the dedicated application config-reader GID (`10001`) as
`root:10001 0750` and `root:10001 0640`, respectively. The preflight also verifies that
the rendered config bind mounts use this exact directory read-only.

`compose.validation.yaml` is a separate loopback-only synthetic wiring contract. It
replaces `app`, `control`, and `worker` with a process that writes explicitly synthetic
ready snapshots and requires `TUDT_SYNTHETIC_READY_VALIDATION=explicit-loopback-non-production`.
It also forces the gateway to `127.0.0.1:${VALIDATION_HTTPS_PORT:-18443}` with internal
TLS. Its PostgreSQL override sets `archive_mode=off` because this local synthetic path
has no initialized pgBackRest stanza or off-host repository; production `compose.yaml`
keeps WAL archiving enabled. Set a free high port explicitly on shared validation hosts.
A PASS proves only
Compose dependency, health-probe, migration, and loopback gateway wiring. It is not
production readiness, Telegram/provider behavior, public TLS, firewall, or deployment
evidence, and the override must never be used for production startup.

The five-minute erasure ledger job requires its own `ERASURE_RESTIC_REPOSITORY` and
three `erasure_*` repository secrets (reader group 21018). Provision and validate the
repository using [the independent ledger runbook](../docs/runbooks/erasure-replica.md)
before enabling its timer. Missing or stale replicas produce a critical monitor alert.
