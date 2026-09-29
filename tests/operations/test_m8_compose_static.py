import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = json.loads((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
BOOTSTRAP_COMPOSE = json.loads((ROOT / "deploy/compose.bootstrap.yaml").read_text(encoding="utf-8"))
SECRET_MANIFEST = json.loads((ROOT / "deploy/config/secret-files.json").read_text(encoding="utf-8"))
PYTHON_SERVICES = ("app", "control", "worker", "migrate")
HEALTHCHECK_SERVICES = (
    "https-gateway",
    "app",
    "control",
    "worker",
    "postgres",
    "redis",
    "ops-monitor",
)


@pytest.mark.unit
def test_production_compose_has_exact_services_network_membership_and_volumes() -> None:
    services = COMPOSE["services"]
    assert set(services) == {
        "https-gateway",
        "app",
        "control",
        "worker",
        "postgres",
        "redis",
        "migrate",
        "session-backup",
        "data-export",
        "erasure-ledger-export",
        "ops-monitor",
    }
    assert COMPOSE["networks"] == {
        "edge": {"driver": "bridge"},
        "backend": {"driver": "bridge"},
        "backup-egress": {"driver": "bridge"},
    }
    assert {name: set(service.get("networks", [])) for name, service in services.items()} == {
        "https-gateway": {"edge"},
        "app": {"backend"},
        "control": {"edge", "backend"},
        "worker": {"backend"},
        "postgres": {"backend", "backup-egress"},
        "redis": {"backend"},
        "migrate": {"backend"},
        "session-backup": {"backup-egress"},
        "data-export": {"backend"},
        "erasure-ledger-export": {"backend", "backup-egress"},
        "ops-monitor": {"backend"},
    }
    assert set(COMPOSE["volumes"]) == {
        "postgres-data",
        "pgbackrest-spool",
        "redis-data",
        "telethon-session",
        "media-data",
        "caddy-data",
    }
    assert services["session-backup"]["profiles"] == ["ops"]
    assert services["data-export"]["profiles"] == ["ops"]
    assert services["erasure-ledger-export"]["profiles"] == ["ops"]
    assert "profiles" not in services["ops-monitor"]


@pytest.mark.unit
def test_gateway_is_the_only_tcp_443_host_port_and_no_internal_service_is_published() -> None:
    services = COMPOSE["services"]
    assert services["https-gateway"]["ports"] == [
        {"target": 8443, "published": "443", "protocol": "tcp"}
    ]
    assert all(
        "ports" not in service for name, service in services.items() if name != "https-gateway"
    )
    rendered = json.dumps(COMPOSE, sort_keys=True)
    assert '"published": "80"' not in rendered
    assert '"protocol": "udp"' not in rendered
    assert "5432:5432" not in rendered
    assert "6379:6379" not in rendered
    for service in services.values():
        if service["user"] != "0":
            assert all(port["target"] >= 1024 for port in service.get("ports", []))
    assert services["https-gateway"]["depends_on"] == {"control": {"condition": "service_started"}}


@pytest.mark.unit
def test_compose_uses_immutable_images_real_entrypoints_and_hardened_services() -> None:
    services = COMPOSE["services"]
    assert services["https-gateway"]["image"].startswith("${GATEWAY_IMAGE:?")
    assert services["redis"]["image"].endswith(
        "@sha256:d9f0312a780ed4ad4c22c05790c20b3902498b563ba01f3f1134678b9bd2f311"
    )
    for name in ("app", "control", "worker", "migrate"):
        assert services[name]["image"].startswith("${APP_IMAGE:?")
        assert services[name]["command"][1].startswith("telegram_userbot.processes.")
        assert "check" not in " ".join(services[name]["command"])
    assert services["postgres"]["image"].startswith("${DATABASE_IMAGE:?")
    expected_database_identities = {
        "app": ("telegram_userbot_app_login", "telegram_userbot_app_runtime"),
        "control": ("telegram_userbot_control_login", "telegram_userbot_control_runtime"),
        "worker": ("telegram_userbot_worker_login", "telegram_userbot_worker_runtime"),
        "migrate": ("telegram_userbot_migrator_login", "telegram_userbot_migrator"),
    }
    for name, (login_role, runtime_role) in expected_database_identities.items():
        assert services[name]["environment"]["DATABASE_USER"] == login_role
        assert services[name]["environment"]["DATABASE_RUNTIME_ROLE"] == runtime_role

    for service in services.values():
        assert service["platform"] == "linux/amd64"
        assert service["user"] != "0"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["pids_limit"] > 0
        assert service["mem_limit"]
        assert service["cpus"] > 0
        assert service["restart"] in {"unless-stopped", "no"}
        assert service["stop_grace_period"]
        assert "/var/run/docker.sock" not in json.dumps(service)
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"
        assert service.get("pid") != "host"

    # The gateway health probe forks BusyBox wget.  PID 1 must reap those
    # children so repeated healthchecks cannot exhaust its bounded PID budget.
    assert services["https-gateway"]["init"] is True
    # Caddy/Tini can retain a sizeable thread/process set on the small VM. Keep
    # a finite but non-starving budget for the native wget health probe and exec.
    assert services["https-gateway"]["pids_limit"] == 256

    for name in HEALTHCHECK_SERVICES:
        assert "healthcheck" in services[name]

    # Python probes need a bounded cold-start allowance on the 2 vCPU target;
    # native probes remain short so dependency failure detection stays prompt.
    for name in ("app", "control", "worker", "ops-monitor"):
        assert services[name]["healthcheck"]["timeout"] == "10s"
    for name in ("https-gateway", "postgres", "redis"):
        assert services[name]["healthcheck"]["timeout"] == "3s"


@pytest.mark.unit
def test_python_services_share_reviewed_config_directory_and_exact_manifest_identity() -> None:
    expected_mount = {
        "type": "bind",
        "source": (
            "${DEPLOYMENT_CONFIG_DIR:?set the reviewed non-secret deployment config directory}"
        ),
        "target": "/etc/telegram-userbot/config",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    for name in PYTHON_SERVICES:
        service = COMPOSE["services"][name]
        assert expected_mount in service["volumes"]
        environment = service["environment"]
        assert environment["CONFIG_FILE"] == "/etc/telegram-userbot/config/deployment.json"
        assert environment["DEPLOYMENT_ID"] == "${DEPLOYMENT_ID:?}"
        assert environment["SOURCE_COMMIT"] == (
            "${SOURCE_COMMIT:?set the exact 40-character source commit}"
        )
        assert environment["PUBLIC_HOST"] == ("${PUBLIC_HOST:?set the canonical public DNS name}")


@pytest.mark.unit
def test_configured_python_process_modules_are_all_importable() -> None:
    configured = {
        service["command"][1]
        for name, service in COMPOSE["services"].items()
        if name in PYTHON_SERVICES
    }
    missing = frozenset(module for module in configured if importlib.util.find_spec(module) is None)
    assert not missing


@pytest.mark.unit
def test_secrets_are_files_with_exact_minimal_service_mounts() -> None:
    services = COMPOSE["services"]
    expected = {item["id"]: set(item["services"]) for item in SECRET_MANIFEST["files"]}
    actual: dict[str, set[str]] = {name: set() for name in COMPOSE["secrets"]}
    for service_name, service in services.items():
        for secret in service.get("secrets", []):
            actual[secret].add(service_name)
    for secret in BOOTSTRAP_COMPOSE["services"]["postgres"]["secrets"]:
        actual[secret].add("postgres-bootstrap")
    nonsecret_shared_groups = {
        "postgres": {"21016"},
        "session-backup": {"21016"},
        "data-export": {"21015"},
        "erasure-ledger-export": {"21015", "21016"},
        "ops-monitor": {"21016"},
    }
    assert actual == {
        consumer: gids | nonsecret_shared_groups.get(consumer, set())
        for consumer, gids in expected.items()
    }
    assert set(COMPOSE["secrets"]) == set(expected)
    for name, source in COMPOSE["secrets"].items():
        assert source == {"file": f"${{SECRET_ROOT:?}}/{name}"}

    for service in services.values():
        for key, value in service.get("environment", {}).items():
            assert "PASSWORD" not in key or key.endswith("_FILE")
            assert not key.endswith("DSN")
            assert not key.endswith("URL") or key == "PUBLIC_BASE_URL"
            assert "@postgres" not in str(value)


@pytest.mark.unit
def test_secret_group_membership_matches_manifest_for_steady_and_bootstrap_consumers() -> None:
    expected: dict[str, set[str]] = {}
    for entry in SECRET_MANIFEST["files"]:
        gid = str(entry["expected_gid"])
        for consumer in entry["services"]:
            expected.setdefault(consumer, set()).add(gid)

    actual = {
        name: set(COMPOSE["services"][name].get("group_add", []))
        for name in (
            "app",
            "control",
            "worker",
            "migrate",
            "redis",
            "postgres",
            "session-backup",
            "data-export",
            "erasure-ledger-export",
            "ops-monitor",
        )
    }
    actual["postgres-bootstrap"] = set(
        BOOTSTRAP_COMPOSE["services"]["postgres"].get("group_add", [])
    )
    non_secret_shared_storage_groups = {
        "postgres": {"21016"},
        "session-backup": {"21016"},
        "data-export": {"21015"},
        "erasure-ledger-export": {"21015", "21016"},
        "ops-monitor": {"21016"},
    }
    assert actual == {
        service: groups | non_secret_shared_storage_groups.get(service, set())
        for service, groups in expected.items()
    }
    assert "_SECRET_GID" not in json.dumps(COMPOSE)
    assert "_SECRET_GID" not in json.dumps(BOOTSTRAP_COMPOSE)


@pytest.mark.unit
def test_non_secret_deployment_example_and_validation_override_are_fail_closed() -> None:
    schema = json.loads((ROOT / "deploy/config/deployment.schema.json").read_text(encoding="utf-8"))
    example = json.loads(
        (ROOT / "deploy/config/deployment.example.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        example
    )
    assert example["deployable"] is False
    assert example["target"]["ubuntu"] == "26.04"
    assert example["target"]["minimum_disk_gib"] == 40
    assert example["target"]["validation_disk_gib"] == 64
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate({**example, "deployable": True})

    override = (ROOT / "deploy/compose.validation.yaml").read_text(encoding="utf-8")
    assert "VALIDATION ONLY" in override
    assert "ports: !override" in override
    assert "target: 8443" in override
    assert "host_ip: 127.0.0.1" in override
    assert 'published: "${VALIDATION_HTTPS_PORT:-18443}"' in override
    assert "TLS_MODE: internal" in override
    assert override.count("telegram_userbot.processes.synthetic_ready") == 3
    assert override.count('BOOTSTRAP_MAINTENANCE: "0"') == 3
    assert override.count("APP_ENV: validation") == 3
    assert override.count("org.telegram-userbot.validation-only") == 8
    assert override.count("org.telegram-userbot.evidence-scope: synthetic-compose-only") == 8
    assert "target: /docker-entrypoint-initdb.d" in override
    assert "source: ./postgres/bootstrap" in override
    assert "explicit validation acknowledgement required" in override
    assert "0.0.0.0" not in override  # noqa: S104 - asserts loopback-only validation binding
    assert 'published: "443"' not in override

    # The validation database has no pgBackRest stanza or off-host target.  Disable
    # WAL archiving in this overlay only; the production command remains archive-on.
    assert "  postgres:\n    # Validation has no initialized pgBackRest stanza" in override
    assert (
        "    command: !override\n      - postgres\n      - -c\n      - archive_mode=off"
    ) in override
    assert "archive_command" not in override
    assert COMPOSE["services"]["postgres"]["command"] == [
        "postgres",
        "-c",
        "archive_mode=on",
        "-c",
        "archive_timeout=300s",
        "-c",
        "archive_command=pgbackrest --config=/run/pgbackrest/pgbackrest.conf "
        "--stanza=telegram-userbot archive-push %p",
    ]

    for name in ("app", "control", "worker"):
        assert COMPOSE["services"][name]["environment"]["BOOTSTRAP_MAINTENANCE"] == (
            "${BOOTSTRAP_MAINTENANCE:-1}"
        )


@pytest.mark.integration
def test_docker_compose_production_bootstrap_and_validation_files_are_parseable() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is not installed on this validation host")
    environment = {
        **os.environ,
        "APP_IMAGE": "registry.invalid/app@sha256:" + "a" * 64,
        "DATABASE_IMAGE": "registry.invalid/database@sha256:" + "b" * 64,
        "GATEWAY_IMAGE": "registry.invalid/gateway@sha256:" + "c" * 64,
        "OPS_IMAGE": "registry.invalid/ops@sha256:" + "e" * 64,
        "COMPOSE_PROJECT_NAME": "telegram-userbot-test",
        "DEPLOYMENT_CONFIG_DIR": str(ROOT / "deploy/config"),
        "DEPLOYMENT_ID": "test-deployment",
        "SOURCE_COMMIT": "d" * 40,
        "PUBLIC_HOST": "validation.example.invalid",
        "ACME_EMAIL": "validation@example.invalid",
        "SECRET_ROOT": str(ROOT / "deploy/config"),
        "OPS_STATE_DIR": str(ROOT / ".pytest-m8-ops-state"),
        "EXPORT_STAGING_DIR": str(ROOT / ".pytest-m8-export-staging"),
        "AGE_RECIPIENT_FILE": str(ROOT / "deploy/config" / "age-recipient.example"),
        "PGBACKREST_S3_ENDPOINT": "s3.example.invalid",
        "PGBACKREST_S3_BUCKET": "telegram-userbot-validation",
        "PGBACKREST_S3_REGION": "us-test-1",
        "SESSION_RESTIC_REPOSITORY": "s3:s3.example.invalid/tudt-validation",
        "VALIDATION_RUN_ID": "static-contract",
        "TUDT_SYNTHETIC_READY_VALIDATION": "explicit-loopback-non-production",
    }
    rendered = subprocess.run(  # noqa: S603 - resolved Docker executable and reviewed arguments
        [
            docker,
            "compose",
            "-f",
            str(ROOT / "deploy/compose.yaml"),
            "-f",
            str(ROOT / "deploy/compose.bootstrap.yaml"),
            "-f",
            str(ROOT / "deploy/compose.validation.yaml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    document = json.loads(rendered.stdout)
    gateway = document["services"]["https-gateway"]
    assert gateway["pids_limit"] == COMPOSE["services"]["https-gateway"]["pids_limit"] == 256
    assert gateway["healthcheck"] == COMPOSE["services"]["https-gateway"]["healthcheck"]
    postgres = document["services"]["postgres"]
    bootstrap_mounts = [
        volume
        for volume in postgres["volumes"]
        if volume.get("target") == "/docker-entrypoint-initdb.d"
    ]
    assert len(bootstrap_mounts) == 1
    bootstrap_mount = bootstrap_mounts[0]
    assert bootstrap_mount["type"] == "bind"
    assert bootstrap_mount["target"] == "/docker-entrypoint-initdb.d"
    assert bootstrap_mount["read_only"] is True
    assert bootstrap_mount["bind"] == {"create_host_path": False}
    assert bootstrap_mount["source"].replace("\\", "/").endswith("/deploy/postgres/bootstrap")
