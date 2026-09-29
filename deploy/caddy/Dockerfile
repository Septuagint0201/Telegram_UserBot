ARG CADDY_BASE="caddy:2.11.4-alpine@sha256:98eb57d882ccd5213d1688764db10c1ca2c58a1ca3a6717a3411ad798f7a423a"

FROM ${CADDY_BASE}

ARG SOURCE_COMMIT
LABEL org.opencontainers.image.title="Telegram Personal AI Digital Twin HTTPS Gateway" \
      org.opencontainers.image.source="https://github.com/Septuagint0201/Telegram_UserBot" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

USER 0:0
RUN test "$(printf '%s' "${SOURCE_COMMIT:-}" | wc -c)" -eq 40 \
    && case "${SOURCE_COMMIT:-}" in *[!0-9a-f]*) exit 1 ;; esac \
    && command -v setcap >/dev/null \
    && command -v getcap >/dev/null \
    && command -v wget >/dev/null \
    && setcap -r /usr/bin/caddy \
    && test -z "$(getcap /usr/bin/caddy)" \
    && chown -R 1000:1000 /data /config \
    && chmod 0750 /data /config

# Caddy listens on the unprivileged container port 8443. Removing the upstream
# file capability lets this process execute under cap_drop=ALL and NNP.
USER 1000:1000
