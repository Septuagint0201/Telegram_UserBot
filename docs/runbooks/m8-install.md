# M8 fresh install — Ubuntu 26.04 amd64

## Scope and stop conditions

This procedure ends at maintenance-ready. It does not authorize real Telegram/provider
traffic, AUTO, public routing, or a production claim. Stop if the host is not Ubuntu
26.04 `amd64`, has less than 2 vCPU/4 GiB RAM, lacks a reviewed 40 GiB allocation for the
profile, lacks the separately planned free space for this execution, lacks synchronized time,
or the source signature/image digests cannot be verified. Profile capacity and current free
space are distinct checks.

The inspected target reported a 60.9 GiB root filesystem with approximately 32.9 GiB free on
2026-09-04. That observation does not establish a 40 GiB constrained-volume profile and is not
enough free space to treat this host as a fresh 40 GiB soak environment. Recheck capacity and
free space immediately before every install, validation, or soak run and record the result with
the candidate evidence.

## Host and source

1. Confirm the exact host, filesystem encryption decision, firewall, NTP, free space, and
   that only the intended SSH/public 443 policy is planned. Do not alter an existing
   reverse proxy or unrelated Compose project.
2. Install a reviewed Docker Engine and Compose plugin version. Do not use convenience
   scripts, floating channels, or Watchtower.
3. Check out the approved signed commit at `/opt/telegram-userbot/releases/<commit>` and
   make `/opt/telegram-userbot/current` point to that reviewed release. Verify the GPG
   signature before pulling images. Resolve that operator-facing symlink once and retain
   the canonical release directory for every source-bound command in this maintenance
   shell:

   ```bash
   release_root="$(readlink -e -- /opt/telegram-userbot/current)" || exit 1
   case "$release_root" in
     /opt/telegram-userbot/releases/*) ;;
     *) printf '%s\n' 'current does not resolve inside the release root' >&2; exit 1 ;;
   esac
   test -d "$release_root" || exit 1
   ```

   If the shell changes, resolve and verify the reviewed target again. Do not replace
   `$release_root` with the `current` symlink in a source-root, import, or Compose command.
4. Ensure the host has the reviewed Ubuntu CPython `3.14` runtime at
   `/usr/bin/python3.14`; do not substitute an unreviewed `python3` alias. Create the
   canonical host-side checker environment and install both hash-locked requirement sets
   from the reviewed checkout:

   ```bash
   sudo /usr/bin/python3.14 -m venv /opt/telegram-userbot/venv
   sudo /opt/telegram-userbot/venv/bin/python -m pip install \
     --require-hashes -r "$release_root/requirements/bootstrap.lock"
   sudo /opt/telegram-userbot/venv/bin/python -m pip install \
     --require-hashes -r "$release_root/requirements/runtime.lock"
   ```

   The preflight imports application code only through the reviewed checkout
   `PYTHONPATH`; it is not an editable host installation.
5. Copy `deploy/config/deployment.example.json` and the secret manifest to
   `/etc/telegram-userbot/config`; replace examples, keep `deployable=false`, and keep the
   exact source commit/schema/image digests. A maintainer changes `deployable` only after
   the candidate image inventory exists.

Create these host-owned locations without symlinks:

```text
/etc/telegram-userbot/config                 root:10001 0750, dedicated config-reader group
/etc/telegram-userbot/secrets                root:root 0700
/etc/telegram-userbot/compose.env            root:root 0600, non-secret interpolation only
/etc/telegram-userbot/ops.env                root:root 0600, based on deploy/systemd/ops.env.example
/var/lib/telegram-userbot/ops-state          root:21016 2770
/var/lib/telegram-userbot/export-staging     root:21015 2770
/var/lib/telegram-userbot/erasure-ledger      root:21015 2770
```

Create each secret as `root:<manifest expected_gid> 0440`; never put secret values in an
environment file. Refuse placeholders, reused secrets, newline-terminated secrets, or a
symlink at any path.

Create `deployment.json`, `secret-files.json`, and every other container-read non-secret
config file as `root:10001 0640`. GID `10001` is reserved for the unprivileged application
config-reader group; it grants read/traverse access to the application, control, worker,
migration, export, and restore-gate containers without making configuration world-readable.

## Content-free preflight and bootstrap

Using the same verified canonical `$release_root`, render only to a root-only temporary
file and never print it. The preflight intentionally rejects a symlink as `--source-root`:

```bash
cd -- "$release_root"
umask 077
set -Eeuo pipefail
compose_config="$(mktemp /tmp/telegram-userbot-compose.XXXXXX.json)"
trap 'rm -f -- "$compose_config"' EXIT
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f deploy/compose.yaml -f deploy/compose.bootstrap.yaml \
  config --format json > "$compose_config"
SECRET_ROOT=/etc/telegram-userbot/secrets \
PYTHONPATH="$release_root/src" \
  /opt/telegram-userbot/venv/bin/python -m telegram_userbot.processes.deployment_preflight \
  --deployment-config /etc/telegram-userbot/config/deployment.json \
  --compose-config "$compose_config" \
  --source-root "$release_root"
```

Stop on any nonzero result. Confirm the selected project name is new and no derived
volume already exists. Before starting, pull only the exact digest references recorded in
the deployment manifest and inspect the resulting image IDs. Once that explicit pull has
passed, every startup command uses `--pull never`; a missing local image is a hard stop,
not permission to resolve a tag or a moving registry reference. Then create the fresh
database only with the bootstrap overlay, run the one-shot migration, and remove bootstrap
credentials from steady rendering:

```bash
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f deploy/compose.yaml -f deploy/compose.bootstrap.yaml pull --policy always \
  postgres redis
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f deploy/compose.yaml -f deploy/compose.bootstrap.yaml up -d --wait --pull never postgres redis
docker compose --env-file /etc/telegram-userbot/compose.env \
  -f deploy/compose.yaml run --rm --no-deps --pull never migrate
```

Do not start `app`, `control`, `worker`, or the gateway if migration, role closure,
schema compatibility, secret preflight, or restore-gate initialization is incomplete.
An M8 synthetic validation stack uses a unique project, fresh volumes, and loopback high
port; it is not this production stack and is not deployment evidence.

## Timers and handoff

Prepare `/etc/telegram-userbot/ops.env`, then install but do not enable timers:

```bash
sudo "$release_root/deploy/systemd/install-systemd.sh"
systemctl list-timers 'tudt-*'
```

After off-host repositories, an independent alert route, and a Session maintenance
window are reviewed, enable with the script's explicit two-part confirmation. Record
source commit, tree, image digests, config hash, schema head, and content-free results.
Public TLS, live account authorization, restore, RPO/RTO, and 24-hour soak remain
`NOT RUN` until separately executed.

The calendar timers use the deployment host's local timezone. The inspected target reported
`Asia/Tokyo` on 2026-09-04; run `timedatectl` and record its result for every operation run.
Installation does not change that timezone, and Telegram account/application timezone settings
do not affect the backup/export/Session windows. Revalidate all timer calendars after any
operator-initiated host timezone change.
