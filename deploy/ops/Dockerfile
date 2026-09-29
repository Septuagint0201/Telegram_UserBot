ARG APP_IMAGE="invalid.invalid/telegram-userbot-app@sha256:0000000000000000000000000000000000000000000000000000000000000000"
FROM ${APP_IMAGE}

ARG SOURCE_COMMIT
LABEL org.opencontainers.image.title="Telegram UserBot one-shot operations" \
      org.opencontainers.image.source="https://github.com/Septuagint0201/Telegram_UserBot" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

USER 0:0
RUN test "$(printf '%s' "${SOURCE_COMMIT:-}" | wc -c)" -eq 40 \
    && case "${SOURCE_COMMIT:-}" in *[!0-9a-f]*) exit 1 ;; esac \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        age=1.1.1-1+b3 \
        restic=0.14.0-1+b5 \
    && rm -rf /var/lib/apt/lists/* \
    && command -v age >/dev/null \
    && command -v restic >/dev/null \
    && mkdir -p /session \
    && chown 10001:10001 /session \
    && chmod 0700 /session

COPY --chmod=0555 deploy/ops/session_backup.py /opt/ops/session_backup.py
COPY --chmod=0555 deploy/ops/session_restore.py /opt/ops/session_restore.py
COPY --chmod=0555 deploy/ops/data_export.py /opt/ops/data_export.py
COPY --chmod=0555 deploy/ops/monitor.py /opt/ops/monitor.py
COPY --chmod=0555 deploy/ops/restore_gate.py /opt/ops/restore_gate.py

COPY --chmod=0555 deploy/ops/erasure_replica.py /opt/ops/erasure_replica.py

USER 10001:10001
ENTRYPOINT ["/opt/venv/bin/python"]
