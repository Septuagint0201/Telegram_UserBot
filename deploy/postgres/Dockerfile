ARG POSTGRES_BASE="pgvector/pgvector:0.8.6-pg17-bookworm@sha256:f193b9c848deb27b0e892f85ed07b53fd83ccb1feb634ba415b1cdc44411bedc"
ARG PGBACKREST_VERSION="2.59.1-1.pgdg12+1"
ARG PGBACKREST_DEB_SHA256="ecea2337fe53ec1f86d87db21ad46747c7ff898bd877288452b90374e80a6fb9"

FROM ${POSTGRES_BASE}

ARG PGBACKREST_VERSION
ARG PGBACKREST_DEB_SHA256
ARG SOURCE_COMMIT
LABEL org.opencontainers.image.title="Telegram UserBot PostgreSQL" \
      org.opencontainers.image.source="https://github.com/Septuagint0201/Telegram_UserBot" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

USER 0:0
# Debian bookworm's 2.45 package predates PostgreSQL 17 and rejects its control-file
# catalog version. PGDG 2.59.1 is the reviewed PG17-compatible package already available
# from the pinned pgvector base image repository. The package archive is hash-checked
# before installation because apt repository metadata is mutable.
RUN test "$(printf '%s' "${SOURCE_COMMIT:-}" | wc -c)" -eq 40 \
    && case "${SOURCE_COMMIT:-}" in *[!0-9a-f]*) exit 1 ;; esac \
    && apt-get update \
    && cd /tmp \
    && apt-get download "pgbackrest=${PGBACKREST_VERSION}" \
    && test "$(find /tmp -maxdepth 1 -type f -name 'pgbackrest_*.deb' | wc -l)" -eq 1 \
    && printf '%s  %s\n' "${PGBACKREST_DEB_SHA256}" "$(find /tmp -maxdepth 1 -type f -name 'pgbackrest_*.deb' -print -quit)" \
        | sha256sum --check --status \
    && apt-get install --yes --no-install-recommends /tmp/pgbackrest_*.deb \
    && pgbackrest version | grep -Fqx 'pgBackRest 2.59.1' \
    && rm -f /tmp/pgbackrest_*.deb \
    && rm -rf /var/lib/apt/lists/* \
    && command -v pgbackrest >/dev/null

COPY --chmod=0555 deploy/postgres/runtime-entrypoint.sh /usr/local/bin/tudt-postgres-entrypoint
COPY --chmod=0555 deploy/postgres/run-backup.sh /usr/local/bin/tudt-pgbackrest-backup
COPY --chmod=0555 deploy/postgres/run-restore.sh /usr/local/bin/tudt-pgbackrest-restore
COPY --chmod=0555 deploy/postgres/run-wal-check.sh /usr/local/bin/tudt-pgbackrest-wal-check

ENTRYPOINT ["/usr/local/bin/tudt-postgres-entrypoint"]
CMD ["postgres"]
