# M8 Operations runbooks

These runbooks target Ubuntu Server 26.04 LTS on `linux/amd64`. They are operator
procedures, not evidence by themselves. Commands that have not been executed against an
exact signed commit and immutable image set remain `NOT RUN`.

- `m8-install.md`: new-host installation through maintenance-ready.
- `m8-upgrade-rollback.md`: reviewed application/database change and rollback boundary.
- `m8-backup-restore.md`: scheduled backup, Session owner gate, and isolated restore.
- `m8-security-patch.md`: Ubuntu, kernel, Docker, and Compose patch window.

Never paste rendered Compose output, secret files, Session data, provider bodies, private
endpoints, or authorization headers into tickets or evidence. Every stop condition is
fail-closed: keep `BOOTSTRAP_MAINTENANCE=1`, do not start `app`, and do not expose the
gateway until the named gate is resolved.
