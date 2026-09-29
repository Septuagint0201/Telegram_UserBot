ARG PYTHON_BASE="python:3.14.7-slim-bookworm@sha256:ff4ceef5258b9303b40c004af0bd31ac82c6248a6b951f9d9b329bf456f1f4b7"

FROM ${PYTHON_BASE} AS runtime-dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build
COPY requirements/runtime.lock /build/requirements/runtime.lock
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install \
        --no-deps \
        --require-hashes \
        --requirement /build/requirements/runtime.lock

FROM ${PYTHON_BASE} AS runtime

ARG SOURCE_COMMIT
LABEL org.opencontainers.image.title="Telegram Personal AI Digital Twin" \
      org.opencontainers.image.source="https://github.com/Septuagint0201/Telegram_UserBot" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/opt/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN test "$(printf '%s' "${SOURCE_COMMIT:-}" | wc -c)" -eq 40 \
    && case "${SOURCE_COMMIT:-}" in *[!0-9a-f]*) exit 1 ;; esac \
    && mkdir -p \
        /opt/app \
        /var/lib/telegram-userbot/runtime \
        /var/lib/telegram-userbot/session \
        /var/lib/telegram-userbot/media \
    && chown -R 10001:10001 /opt/app /var/lib/telegram-userbot

COPY --from=runtime-dependencies /opt/venv /opt/venv
COPY --chown=10001:10001 src /opt/app/src
COPY --chown=10001:10001 alembic /opt/app/alembic
COPY --chown=10001:10001 alembic.ini /opt/app/alembic.ini
COPY --chown=10001:10001 \
    deploy/postgres/m1_roles.sql \
    deploy/postgres/m2_roles.sql \
    deploy/postgres/m3_roles.sql \
    deploy/postgres/m4_roles.sql \
    deploy/postgres/m5_roles.sql \
    deploy/postgres/m6_roles.sql \
    deploy/postgres/m7_roles.sql \
    deploy/postgres/m8_roles.sql \
    /opt/app/deploy/postgres/
COPY --chown=10001:10001 requirements/runtime.lock /opt/app/requirements/runtime.lock
COPY --chown=10001:10001 DISCLOSURE /opt/app/DISCLOSURE
COPY --chown=10001:10001 deploy/sbom/generate_python_inventory.py /opt/app/deploy/sbom/
COPY --chown=10001:10001 deploy/sbom/python-inventory.schema.json /opt/app/deploy/sbom/

WORKDIR /opt/app
USER 10001:10001

# Process-specific commands and healthchecks are supplied by the reviewed
# production Compose definition. The image itself must not start a fake
# readiness command or the configuration-only bootstrap checker.
ENTRYPOINT ["/opt/venv/bin/python"]
