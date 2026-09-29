from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _make_evidence_directory(path: Path) -> Path:
    """Construct the host-owned operations directory expected by restore code."""

    path.mkdir()
    if os.name != "posix":
        return path
    geteuid = getattr(os, "geteuid", None)
    chown = getattr(os, "chown", None)
    if not callable(geteuid) or geteuid() != 0 or not callable(chown):
        pytest.skip("restore evidence contract requires a root test process")
    try:
        chown(path, 0, 21016)
        path.chmod(0o2770)
    except OSError as error:
        pytest.skip(f"restore evidence contract unavailable: {type(error).__name__}")
    return path


def _write_session_approval(path: Path, value: str) -> None:
    path.write_text(value, encoding="ascii")
    if os.name != "posix":
        return
    geteuid = getattr(os, "geteuid", None)
    chown = getattr(os, "chown", None)
    if not callable(geteuid) or geteuid() != 0 or not callable(chown):
        pytest.skip("session approval contract requires a root test process")
    try:
        chown(path, 0, 0)
        path.chmod(0o400)
    except OSError as error:
        pytest.skip(f"session approval contract unavailable: {type(error).__name__}")


def _write_root_owned_file(path: Path, value: str, mode: int = 0o600) -> Path:
    """Create a strict host input for restore tests on POSIX runners."""

    path.write_text(value, encoding="ascii")
    if os.name != "posix":
        return path
    geteuid = getattr(os, "geteuid", None)
    chown = getattr(os, "chown", None)
    if not callable(geteuid) or geteuid() != 0 or not callable(chown):
        pytest.skip("restore input contract requires a root test process")
    try:
        chown(path, 0, 0)
        path.chmod(mode)
    except OSError as error:
        pytest.skip(f"restore input contract unavailable: {type(error).__name__}")
    return path


@pytest.fixture
def restore_compose_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Model installed root-owned inputs without changing checkout ownership."""

    base, overlay, validation = (
        _write_root_owned_file(
            tmp_path / name, (ROOT / "deploy" / name).read_text(encoding="ascii")
        )
        for name in ("compose.yaml", "compose.restore.yaml", "compose.restore.validation.yaml")
    )
    return base, overlay, validation


def _load_script(name: str) -> ModuleType:
    path = ROOT / "deploy" / "ops" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"m8_restore_test_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_restore_deployment(path: Path) -> tuple[Path, dict[str, str]]:
    """Create a non-secret, deployable image contract for synthetic runner tests."""

    document = json.loads(
        (ROOT / "deploy/config/deployment.example.json").read_text(encoding="utf-8")
    )
    document["deployable"] = True
    document["deployment_id"] = "restore-test-primary"
    document["source_commit"] = "1" * 40
    document["public_host"] = "restore.example.test"
    references = {
        "application": "registry.example/app@sha256:" + "1" * 64,
        "database": "registry.example/database@sha256:" + "2" * 64,
        "gateway": "registry.example/gateway@sha256:" + "3" * 64,
        "redis": "registry.example/redis@sha256:" + "4" * 64,
        "session-backup": "registry.example/ops@sha256:" + "5" * 64,
        "data-export": "registry.example/ops@sha256:" + "5" * 64,
    }
    document["images"] = {
        name: {"status": "BUILT", "reference": reference} for name, reference in references.items()
    }
    return _write_root_owned_file(path, json.dumps(document), mode=0o640), references


def _rendered_restore_services(module: ModuleType, references: dict[str, str]) -> dict[str, object]:
    return {
        "services": {
            service: {"image": references[artifact]}
            for service, artifact in module._RENDERED_SERVICE_ARTIFACTS.items()
        }
    }


@pytest.mark.unit
def test_restore_overlay_is_maintenance_only_and_has_no_public_boundary() -> None:
    overlay = (ROOT / "deploy/compose.restore.yaml").read_text(encoding="utf-8")
    assert "RESTORE ONLY" in overlay
    assert overlay.count('BOOTSTRAP_MAINTENANCE: "1"') == 3
    for service in (
        "postgres-restore",
        "session-restore",
        "restore-gate-close",
        "restore-gate-open",
    ):
        assert f"  {service}:" in overlay
    assert overlay.count('profiles: ["restore"]') == 4
    assert "/usr/local/bin/tudt-pgbackrest-restore" in overlay
    assert "/opt/ops/session_restore.py" in overlay
    assert "/opt/ops/restore_gate.py" in overlay
    assert "TUDT_RESTORE_ONLY: explicit-restore-only" in overlay
    assert "ERASURE_LEDGER_SHA256" in overlay
    assert "credential_master_keyring" in overlay
    assert "erasure_hmac_key" in overlay
    assert "ports:" not in overlay
    assert "privileged:" not in overlay
    assert "/var/run/docker.sock" not in overlay
    # Restore never starts or reconfigures the public gateway; its healthcheck
    # and finite PID budget therefore remain inherited from the reviewed base.
    assert "https-gateway:" not in overlay
    assert "healthcheck:" not in overlay

    validation_overlay = (ROOT / "deploy/compose.restore.validation.yaml").read_text(
        encoding="utf-8"
    )
    assert "https-gateway:" not in validation_overlay
    assert "healthcheck:" not in validation_overlay


@pytest.mark.unit
def test_session_operations_require_the_app_runtime_session_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful restore must produce the exact path used by the app runtime."""

    backup = _load_script("session_backup")
    restore = _load_script("session_restore")
    gate = _load_script("restore_gate")
    assert backup.SESSION_FILENAME == restore.SESSION_FILENAME == gate._SESSION_FILENAME
    assert backup.SESSION_FILENAME == "account.session"
    app_runtime = (ROOT / "src/telegram_userbot/processes/app.py").read_text(encoding="utf-8")
    assert 'SESSION_PATH = Path("/var/lib/telegram-userbot/session/account.session")' in app_runtime

    session_root = tmp_path / "session"
    session_root.mkdir()
    monkeypatch.setattr(backup, "SESSION_ROOT", session_root)
    monkeypatch.setattr(backup, "SESSION_PATH", session_root / backup.SESSION_FILENAME)
    (session_root / "other.session").write_bytes(b"synthetic")
    with pytest.raises(ValueError, match=r"^SESSION_INVENTORY_INVALID$"):
        backup._session_file()
    (session_root / "other.session").unlink()
    expected = session_root / backup.SESSION_FILENAME
    expected.write_bytes(b"synthetic")
    assert backup._session_file() == expected

    restored_root = tmp_path / "restored-session"
    restored_root.mkdir()
    (restored_root / "other.session").write_bytes(b"synthetic")
    with pytest.raises(ValueError, match=r"^RESTORED_SESSION_INVENTORY_INVALID$"):
        restore._restored_session(restored_root)
    (restored_root / "other.session").unlink()
    restored_expected = restored_root / restore.SESSION_FILENAME
    restored_expected.write_bytes(b"synthetic")
    assert restore._restored_session(restored_root) == restored_expected


@pytest.mark.unit
def test_restore_image_contract_covers_base_and_restore_helpers(tmp_path: Path) -> None:
    module = _load_script("run_restore")
    _deployment_path, references = _write_restore_deployment(tmp_path / "deployment.json")
    deployment_id, loaded = module._load_deployment_contract(_deployment_path)

    assert deployment_id == "restore-test-primary"
    assert loaded == references
    module._validate_rendered_compose_images(
        _rendered_restore_services(module, references),
        deployment_images=loaded,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("tagged-postgres", "COMPOSE_IMAGE_INVALID"),
        ("wrong-gate-image", "COMPOSE_IMAGE_MISMATCH"),
        ("missing-session-helper", "COMPOSE_SERVICE_INVENTORY_MISMATCH"),
    ],
)
def test_restore_image_contract_rejects_unsafe_or_mismatched_helpers(
    tmp_path: Path, mutation: object, error: str
) -> None:
    module = _load_script("run_restore")
    _deployment_path, references = _write_restore_deployment(tmp_path / "deployment.json")
    services = _rendered_restore_services(module, references)["services"]
    assert isinstance(services, dict)
    if mutation == "tagged-postgres":
        services["postgres-restore"] = {"image": "registry.example/db:latest"}
    elif mutation == "wrong-gate-image":
        services["restore-gate-open"] = {"image": "registry.example/ops@sha256:" + "6" * 64}
    else:
        services.pop("session-restore")

    with pytest.raises(module.RestoreOrchestrationError, match=f"^{error}$"):
        module._validate_rendered_compose_images(
            {"services": services},
            deployment_images=references,
        )


@pytest.mark.unit
def test_postgres_restore_wrapper_skips_official_initdb_only_for_exact_restore_command() -> None:
    wrapper = (ROOT / "deploy/postgres/runtime-entrypoint.sh").read_text(encoding="utf-8")
    assert 'TUDT_RESTORE_ONLY:-}" == "explicit-restore-only"' in wrapper
    assert '"$#" -eq 1 && "$1" == "/usr/local/bin/tudt-pgbackrest-restore"' in wrapper
    assert 'exec /usr/local/bin/docker-entrypoint.sh "$@"' in wrapper


@pytest.mark.unit
def test_restore_runner_derives_every_project_object_and_rejects_unsafe_target() -> None:
    module = _load_script("run_restore")
    module._require_root_runtime = lambda: None  # type: ignore[attr-defined]
    names = module._declared_project_objects(
        ROOT / "deploy/compose.yaml", "tudt-restore-contract-a"
    )
    assert names == tuple(
        sorted(
            (kind, f"tudt-restore-contract-a_{name}")
            for kind, name in (
                ("network", "edge"),
                ("network", "backend"),
                ("network", "backup-egress"),
                ("volume", "postgres-data"),
                ("volume", "pgbackrest-spool"),
                ("volume", "redis-data"),
                ("volume", "telethon-session"),
                ("volume", "media-data"),
                ("volume", "caddy-data"),
            )
        )
    )
    local_names = module._declared_project_objects(
        ROOT / "deploy/compose.yaml",
        "tudt-restore-contract-a",
        extra_volume_names=(
            "pgbackrest-validation-repository",
            "restic-validation-repository",
        ),
    )
    assert ("volume", "tudt-restore-contract-a_pgbackrest-validation-repository") in local_names
    assert ("volume", "tudt-restore-contract-a_restic-validation-repository") in local_names
    with pytest.raises(module.RestoreOrchestrationError, match="NEW_PROJECT_CONFIRMATION_REQUIRED"):
        module.run_restore(
            compose_file=ROOT / "deploy/compose.yaml",
            restore_overlay=ROOT / "deploy/compose.restore.yaml",
            env_file=ROOT / "deploy/config/deployment.example.json",
            deployment_config=ROOT / "deploy/config/deployment.example.json",
            project_name="production",
            confirmation="production",
            postgres_backup_set="20260831-020000F",
            session_snapshot_id="a" * 8,
            erasure_ledger=ROOT / "DISCLOSURE",
            erasure_ledger_sha256="b" * 64,
            evidence_directory=ROOT,
        )


@pytest.mark.unit
def test_restore_runner_executes_fixed_stages_and_writes_content_free_terminal_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_compose_files: tuple[Path, Path, Path]
) -> None:
    module = _load_script("run_restore")
    deployment_config, deployment_images = _write_restore_deployment(tmp_path / "deployment.json")
    env_file = _write_root_owned_file(tmp_path / "compose.env", "NON_SECRET=example\n")
    ledger = _write_root_owned_file(tmp_path / "ledger.jsonl", '{"kind":"synthetic-contract"}\n')
    evidence = _make_evidence_directory(tmp_path / "ops-state")
    commands: list[tuple[str, ...]] = []
    seen_environment: dict[str, str] = {}
    rendered = _rendered_restore_services(module, deployment_images)

    def fake_run(
        command: tuple[str, ...] | list[str],
        *,
        environment: dict[str, str],
        timeout: int,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del timeout, check
        commands.append(tuple(command))
        seen_environment.update(environment)
        if tuple(command)[-len(module._COMPOSE_CONFIG_ARGS) :] == module._COMPOSE_CONFIG_ARGS:
            output = json.dumps(rendered)
        else:
            output = (
                "postgres\nredis\n"
                if list(command)[-4:] == ["ps", "--services", "--status", "running"]
                else ""
            )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(module, "_require_root_runtime", lambda: None)
    monkeypatch.setattr(module, "_assert_new_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_run", fake_run)
    digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
    module.run_restore(
        compose_file=restore_compose_files[0],
        restore_overlay=restore_compose_files[1],
        env_file=env_file,
        deployment_config=deployment_config,
        project_name="tudt-restore-contract-a",
        confirmation="tudt-restore-contract-a",
        postgres_backup_set="20260831-020000F_20260901-020000D",
        session_snapshot_id="a" * 8,
        erasure_ledger=ledger,
        erasure_ledger_sha256=digest,
        evidence_directory=evidence,
    )

    assert len(commands) == 9
    assert commands[0][-3:] == ("config", "--format", "json")
    assert commands[-1][-4:] == ("ps", "--services", "--status", "running")
    assert [
        json.loads(
            (evidence / "restore-orchestrations" / "tudt-restore-contract-a" / path).read_text(
                encoding="ascii"
            )
        )["stage"]
        for path in (
            "00-target-verified-new.json",
            "01-compose-validated.json",
            "02-postgres-restored.json",
            "03-session-restored.json",
            "04-dependencies-ready.json",
            "05-schema-migrated.json",
            "06-restore-gate-closed.json",
            "07-erasure-replay-staged.json",
            "08-restore-gate-open.json",
            "09-runtime-set-verified.json",
        )
    ] == [
        "target-verified-new",
        "compose-validated",
        "postgres-restored",
        "session-restored",
        "dependencies-ready",
        "schema-migrated",
        "restore-gate-closed",
        "erasure-replay-staged",
        "restore-gate-open",
        "runtime-set-verified",
    ]
    assert all("down" not in command for command in commands)
    assert seen_environment["BOOTSTRAP_MAINTENANCE"] == "1"
    evidence_run = evidence / "restore-orchestrations" / "tudt-restore-contract-a"
    manifest = json.loads((evidence_run / "manifest.json").read_text(encoding="ascii"))
    marker = json.loads((evidence_run / "09-runtime-set-verified.json").read_text(encoding="ascii"))
    assert marker == {
        "schema_version": 1,
        "kind": "restore-orchestration",
        "deployment_id": "restore-test-primary",
        "project_name": "tudt-restore-contract-a",
        "evidence_scope": "production-off-host",
        "sequence": 9,
        "stage": "runtime-set-verified",
        "result": "PASS",
        "code": "ONLY_POSTGRES_REDIS_RUNNING",
        "previous_sha256": marker["previous_sha256"],
        "recorded_at": marker["recorded_at"],
    }
    assert manifest["result"] == "PASS"
    assert manifest["stage_count"] == 10
    assert manifest["chain_head_sha256"] == manifest["stages"][-1]["sha256"]
    assert [item["sequence"] for item in manifest["stages"]] == list(range(10))
    for previous, current in zip(manifest["stages"], manifest["stages"][1:], strict=False):
        stage = json.loads((evidence_run / current["path"]).read_text(encoding="ascii"))
        assert stage["previous_sha256"] == previous["sha256"]
    manifest_digest = hashlib.sha256((evidence_run / "manifest.json").read_bytes()).hexdigest()
    assert (evidence_run / "manifest.sha256").read_text(encoding="ascii") == (
        f"{manifest_digest}  manifest.json\n"
    )
    serialized = json.dumps(marker, sort_keys=True).lower()
    for forbidden in ("backup_set", "snapshot_id", "ledger_sha", "credential", "session_data"):
        assert forbidden not in serialized

    with pytest.raises(
        module.RestoreOrchestrationError,
        match=r"^EVIDENCE_PROJECT_ALREADY_RECORDED$",
    ):
        module.run_restore(
            compose_file=restore_compose_files[0],
            restore_overlay=restore_compose_files[1],
            env_file=env_file,
            deployment_config=deployment_config,
            project_name="tudt-restore-contract-a",
            confirmation="tudt-restore-contract-a",
            postgres_backup_set="20260831-020000F_20260901-020000D",
            session_snapshot_id="a" * 8,
            erasure_ledger=ledger,
            erasure_ledger_sha256=digest,
            evidence_directory=evidence,
        )


@pytest.mark.unit
def test_restore_runner_recloses_gate_when_final_runtime_inventory_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_compose_files: tuple[Path, Path, Path]
) -> None:
    module = _load_script("run_restore")
    deployment_config, deployment_images = _write_restore_deployment(tmp_path / "deployment.json")
    env_file = _write_root_owned_file(tmp_path / "compose.env", "NON_SECRET=example\n")
    ledger = _write_root_owned_file(tmp_path / "ledger.jsonl", '{"kind":"synthetic-contract"}\n')
    evidence = _make_evidence_directory(tmp_path / "ops-state")
    commands: list[tuple[str, ...]] = []
    rendered = _rendered_restore_services(module, deployment_images)

    def fake_run(
        command: tuple[str, ...] | list[str],
        *,
        environment: dict[str, str],
        timeout: int,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout, check
        normalized = tuple(command)
        commands.append(normalized)
        if normalized[-len(module._COMPOSE_CONFIG_ARGS) :] == module._COMPOSE_CONFIG_ARGS:
            output = json.dumps(rendered)
        else:
            output = (
                "app\npostgres\nredis\n"
                if normalized[-4:]
                == (
                    "ps",
                    "--services",
                    "--status",
                    "running",
                )
                else ""
            )
        return subprocess.CompletedProcess(normalized, 0, output, "")

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(module, "_require_root_runtime", lambda: None)
    monkeypatch.setattr(module, "_assert_new_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_run", fake_run)
    digest = hashlib.sha256(ledger.read_bytes()).hexdigest()

    with pytest.raises(module.RestoreOrchestrationError, match="RESTORE_RUNTIME_SET_INVALID"):
        module.run_restore(
            compose_file=restore_compose_files[0],
            restore_overlay=restore_compose_files[1],
            env_file=env_file,
            deployment_config=deployment_config,
            project_name="tudt-restore-contract-b",
            confirmation="tudt-restore-contract-b",
            postgres_backup_set="20260831-020000F",
            session_snapshot_id="a" * 8,
            erasure_ledger=ledger,
            erasure_ledger_sha256=digest,
            evidence_directory=evidence,
        )

    runtime_index = next(
        index
        for index, command in enumerate(commands)
        if command[-4:] == ("ps", "--services", "--status", "running")
    )
    reclose_indices = [
        index for index, command in enumerate(commands) if command[-1] == "restore-gate-close"
    ]
    assert len(reclose_indices) == 2
    assert reclose_indices[-1] > runtime_index
    evidence_run = evidence / "restore-orchestrations" / "tudt-restore-contract-b"
    manifest = json.loads((evidence_run / "manifest.json").read_text(encoding="ascii"))
    assert manifest["result"] == "FAIL"
    assert manifest["stages"][-1]["stage"] == "runtime-set-verified"
    assert manifest["stages"][-1]["result"] == "FAIL"


@pytest.mark.unit
def test_local_restore_requires_third_overlay_and_exact_synthetic_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_compose_files: tuple[Path, Path, Path]
) -> None:
    module = _load_script("run_restore")
    deployment_config, _ = _write_restore_deployment(tmp_path / "deployment.json")
    env_file = _write_root_owned_file(tmp_path / "compose.env", "NON_SECRET=example\n")
    ledger = _write_root_owned_file(tmp_path / "ledger.jsonl", '{"kind":"synthetic-contract"}\n')
    evidence = _make_evidence_directory(tmp_path / "ops-state")
    digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
    common: dict[str, Any] = {
        "compose_file": restore_compose_files[0],
        "restore_overlay": restore_compose_files[1],
        "env_file": env_file,
        "deployment_config": deployment_config,
        "project_name": "tudt-restore-local-a",
        "confirmation": "tudt-restore-local-a",
        "postgres_backup_set": "20260831-020000F",
        "session_snapshot_id": "a" * 8,
        "erasure_ledger": ledger,
        "erasure_ledger_sha256": digest,
        "evidence_directory": evidence,
    }
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(module, "_require_root_runtime", lambda: None)

    with pytest.raises(module.RestoreOrchestrationError, match="LOCAL_VALIDATION_ACK_REQUIRED"):
        module.run_restore(
            **common,
            validation_overlay=restore_compose_files[2],
        )
    with pytest.raises(
        module.RestoreOrchestrationError,
        match="LOCAL_VALIDATION_OVERLAY_REQUIRED",
    ):
        module.run_restore(
            **common,
            local_validation_ack=module._LOCAL_VALIDATION_ACK,
        )


@pytest.mark.unit
def test_restore_gate_blocks_every_unreconciled_side_effect_surface() -> None:
    module = _load_script("restore_gate")

    class _Result:
        def __init__(self, value: int) -> None:
            self._value = value

        def fetchone(self) -> dict[str, int]:
            return {"pending": self._value, "invalid": self._value}

    class _Connection:
        def __init__(self, match: str | None = None) -> None:
            self.match = match
            self.queries: list[str] = []

        def execute(self, query: str, _parameters: object = None) -> _Result:
            normalized = " ".join(query.split())
            self.queries.append(normalized)
            return _Result(1 if self.match and self.match in normalized else 0)

    clean = _Connection()
    module._verify_side_effect_reconciliation(clean, uuid4())
    rendered = "\n".join(clean.queries)
    for surface in (
        "outbound_delivery_groups",
        "outbound_intents",
        "outbound_attempts",
        "copilot_drafts",
        "proactive_budget_reservations",
        "context_preview_requests",
        "context_preview_deliveries",
        "control_bot_update_receipts",
        "model_run_attempts",
        "model_runs",
    ):
        assert surface in rendered

    with pytest.raises(
        ValueError,
        match=r"^PROACTIVE_BUDGET_RECONCILIATION_REQUIRED$",
    ):
        module._verify_side_effect_reconciliation(
            _Connection("proactive_budget_reservations"), uuid4()
        )


@pytest.mark.unit
def test_restore_memory_redaction_clears_accepted_proposal_body_without_rewriting_audit_state() -> (
    None
):
    module = _load_script("restore_gate")

    class _Connection:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute(self, query: str, _parameters: object = None) -> object:
            self.queries.append(" ".join(query.split()))
            return object()

    connection = _Connection()
    module._redact_memory_scope(
        connection,
        account_id=uuid4(),
        memory={"id": uuid4(), "contact_id": None, "conversation_id": uuid4()},
        completed_at=object(),
    )
    proposal_queries = [query for query in connection.queries if "memory_proposals SET" in query]
    assert len(proposal_queries) == 1
    proposal_query = proposal_queries[0]
    assert "proposed_payload = '{}'::jsonb" in proposal_query
    assert "proposed_text = NULL" in proposal_query
    assert "CASE WHEN state = 'accepted' THEN state ELSE 'invalidated' END" in proposal_query
    assert "state <> 'accepted'" not in proposal_query


@pytest.mark.unit
def test_restore_ledger_is_versioned_injectable_and_unknown_scope_fails_closed(
    tmp_path: Path,
) -> None:
    module = _load_script("restore_gate")
    deployment_id = "production-primary"
    account_id = uuid4()
    secret = b"s" * 32

    def write_ledger(*, version: int, scope: str) -> tuple[Path, str]:
        path = tmp_path / f"ledger-{version}-{scope}.jsonl"
        records = (
            {
                "kind": "header",
                "schema_version": version,
                "deployment_id": deployment_id,
                "account_scope_hmac": hmac.new(secret, account_id.bytes, "sha256").hexdigest(),
                "snapshot_id": "synthetic-ledger-a",
                "exported_at": "2026-09-01T00:00:00+00:00",
            },
            {
                "kind": "erasure",
                "request_id": str(uuid4()),
                "scope_type": scope,
                "target_scope_hmac": "a" * 64,
                "request_idempotency_key": "b" * 64,
                "policy_version": 1,
                "completed_at": "2026-09-01T00:00:00+00:00",
            },
        )
        path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    unknown_version, unknown_version_digest = write_ledger(version=2, scope="memory")
    with pytest.raises(ValueError, match=r"^LEDGER_VERSION_UNSUPPORTED$"):
        module._load_ledger(
            unknown_version,
            expected_digest=unknown_version_digest,
            deployment_id=deployment_id,
            account_id=account_id,
            scope_secret=secret,
        )

    unknown_scope, unknown_scope_digest = write_ledger(version=1, scope="contact")
    with pytest.raises(ValueError, match=r"^LEDGER_SCOPE_UNSUPPORTED$"):
        module._load_ledger(
            unknown_scope,
            expected_digest=unknown_scope_digest,
            deployment_id=deployment_id,
            account_id=account_id,
            scope_secret=secret,
        )

    _snapshot_id, entries = module._load_ledger(
        unknown_scope,
        expected_digest=unknown_scope_digest,
        deployment_id=deployment_id,
        account_id=account_id,
        scope_secret=secret,
        supported_scopes=frozenset({"contact"}),
    )
    observed: list[tuple[object, ...]] = []

    def replay_contact(
        connection: object, replay_account: object, key: bytes, scoped: tuple[object, ...]
    ) -> None:
        observed.append((connection, replay_account, key, scoped))

    sentinel = object()
    module._replay_ledger(
        sentinel,
        account_id=account_id,
        scope_secret=secret,
        entries=entries,
        replayers={"contact": replay_contact},
    )
    assert observed == [(sentinel, account_id, secret, entries)]


@pytest.mark.unit
def test_erasure_ledger_export_is_content_free_and_round_trips_through_loader(
    tmp_path: Path,
) -> None:
    module = _load_script("restore_gate")
    account_id = uuid4()
    secret = b"s" * 32
    completed_at = datetime(2030, 1, 1, tzinfo=UTC)
    account_hmac = module.hmac.new(secret, account_id.bytes, "sha256").digest()
    target_hmac = module.hmac.new(secret, uuid4().bytes, "sha256").digest()
    rows = [
        {
            "account_scope_hmac": account_hmac,
            "scope_type": "memory",
            "target_scope_hmac": target_hmac,
            "request_id": uuid4(),
            "policy_version": 1,
            "completed_at": completed_at,
            "request_idempotency_key": b"i" * 32,
        }
    ]
    payload = module._ledger_document(
        deployment_id="production-primary",
        account_id=account_id,
        scope_secret=secret,
        snapshot_id="ledger-test-a",
        rows=rows,
    )
    output = tmp_path / "ledger.jsonl"
    module._write_ledger_document(output, payload)
    text = output.read_text(encoding="ascii")
    assert "memory" in text
    assert "account_id" not in text
    snapshot_id, entries = module._load_ledger(
        output,
        expected_digest=module.hashlib.sha256(payload).hexdigest(),
        deployment_id="production-primary",
        account_id=account_id,
        scope_secret=secret,
    )
    assert snapshot_id == "ledger-test-a"
    assert len(entries) == 1


@pytest.mark.unit
def test_empty_erasure_ledger_export_still_has_keyed_account_header(tmp_path: Path) -> None:
    module = _load_script("restore_gate")
    account_id = uuid4()
    secret = b"s" * 32
    payload = module._ledger_document(
        deployment_id="production-primary",
        account_id=account_id,
        scope_secret=secret,
        snapshot_id="ledger-empty-a",
        rows=(),
    )
    output = tmp_path / "empty-ledger.jsonl"
    module._write_ledger_document(output, payload)
    snapshot_id, entries = module._load_ledger(
        output,
        expected_digest=module.hashlib.sha256(payload).hexdigest(),
        deployment_id="production-primary",
        account_id=account_id,
        scope_secret=secret,
    )
    assert snapshot_id == "ledger-empty-a"
    assert entries == ()


@pytest.mark.unit
def test_session_maintenance_approval_is_explicit_one_use(tmp_path: Path) -> None:
    module = _load_script("run_maintenance")
    approval = tmp_path / "approval"
    assert module._consume_session_approval("production-primary", approval) is False
    _write_session_approval(approval, "SESSION_BACKUP_APPROVED:production-primary\n")
    assert module._consume_session_approval("production-primary", approval) is True
    assert not approval.exists()

    _write_session_approval(approval, "SESSION_BACKUP_APPROVED:other\n")
    with pytest.raises(module.MaintenanceError, match="SESSION_MAINTENANCE_APPROVAL_INVALID"):
        module._consume_session_approval("production-primary", approval)


@pytest.mark.unit
def test_systemd_contract_has_all_schedules_lock_timeout_and_failure_alert() -> None:
    root = ROOT / "deploy/systemd"
    service = (root / "tudt-ops@.service").read_text(encoding="utf-8")
    assert "OnFailure=tudt-ops-alert@%i.service" in service
    assert "OnFailure=tudt-ops-alert@%n.service" not in service
    assert "ConditionPathExists=/etc/telegram-userbot/ops.env" in service
    assert "ConditionPathExists=/etc/telegram-userbot/compose.env" in service
    assert "ConditionPathExists=/etc/telegram-userbot/config/deployment.json" in service
    assert "ConditionPathIsDirectory=/opt/telegram-userbot/current" in service
    assert "ConditionPathExists=/run/docker.sock" in service
    assert "ReadOnlyPaths=/run/docker.sock" in service
    assert "BindReadOnlyPaths=/run/docker.sock" in service
    assert "PrivateNetwork=true" in service
    assert "Environment=DOCKER_HOST=unix:///run/docker.sock" in service
    assert "UnsetEnvironment=DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH" in service
    assert "/usr/bin/flock --nonblock /run/lock/telegram-userbot-ops.lock" in service
    assert (
        "/opt/telegram-userbot/venv/bin/python "
        "/opt/telegram-userbot/current/deploy/ops/run_maintenance.py"
    ) in service
    assert (
        "/usr/bin/python3 /opt/telegram-userbot/current/deploy/ops/run_maintenance.py"
    ) not in service
    assert "TimeoutStartSec=2h15min" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "run_maintenance.py %i" in service
    assert "docker compose" not in service

    timers = {
        "tudt-postgres-full.timer": "OnCalendar=Sun *-*-* 02:00:00",
        "tudt-postgres-diff.timer": "OnCalendar=Mon..Sat *-*-* 02:00:00",
        "tudt-wal-check.timer": "OnUnitActiveSec=5min",
        "tudt-session-maintenance.timer": "OnCalendar=*-*-* 04:30:00",
        "tudt-data-export.timer": "OnCalendar=*-*-* 03:30:00",
        "tudt-erasure-ledger.timer": "OnUnitActiveSec=5min",
    }
    for name, schedule in timers.items():
        body = (root / name).read_text(encoding="utf-8")
        assert schedule in body
        assert "Unit=tudt-ops@" in body
        assert "WantedBy=timers.target" in body

    installer = (root / "install-systemd.sh").read_text(encoding="utf-8")
    for expected in (
        '"${VERSION_ID:-}" == 26.04',
        '"$(dpkg --print-architecture)" == amd64',
        "[[ -x /opt/telegram-userbot/venv/bin/python ]]",
        "PREFLIGHT_VENV_UNAVAILABLE",
        "PREFLIGHT_PYTHON_UNSUPPORTED",
    ):
        assert expected in installer
    assert "I_ACCEPT_SCHEDULED_OPERATIONS" in installer
    assert "tudt-erasure-ledger.timer" in installer
    assert "systemd-analyze verify" in installer
    assert installer.count("systemd-analyze calendar") == 4
    assert "systemctl show --property=OnFailure --value" in installer
    assert "ON_FAILURE_EXPANSION_INVALID" in installer
    assert "DOCKER_SOCKET_OWNER_INVALID" in installer
    assert "DEPLOYMENT_CONFIG_DIRECTORY_PERMISSION_INVALID" in installer
    assert '"$(stat -c \'%u:%g:%a\' /etc/telegram-userbot/config)" == "0:10001:750"' in installer
    assert '"$deployment_mode" == "0:10001:640"' in installer
    assert "SECRET_MANIFEST_PERMISSION_INVALID" in installer
    assert 'secret-files.json)" == "0:10001:640"' in installer

    backup_runbook = (ROOT / "docs/runbooks/m8-backup-restore.md").read_text(encoding="utf-8")
    assert "Asia/Tokyo" in backup_runbook
    assert "account/application timezone" in backup_runbook
    assert "does not change the host timezone" in backup_runbook
    assert "manifest.sha256" in backup_runbook
    assert "exactly `postgres` and `redis`" in backup_runbook
    assert "local-synthetic-only" in backup_runbook


@pytest.mark.unit
def test_backup_restore_scripts_and_runbooks_preserve_fail_closed_boundaries() -> None:
    backup = (ROOT / "deploy/postgres/run-backup.sh").read_text(encoding="utf-8")
    restore = (ROOT / "deploy/postgres/run-restore.sh").read_text(encoding="utf-8")
    wal = (ROOT / "deploy/postgres/run-wal-check.sh").read_text(encoding="utf-8")
    assert "--no-expire-auto backup" in backup
    assert backup.index(" check >/dev/null") < backup.index(" expire >/dev/null")
    assert "TARGET_NOT_EMPTY" in restore
    assert '--set="$backup_set"' in restore
    assert "--type=immediate --target-action=promote restore" in restore
    assert 'fail "RESTORE_EXECUTION_FAILED"' in restore
    assert "pg_controldata" in restore
    assert "ARCHIVE_STALE" in wal
    assert "interval '15 minutes'" in wal

    runbooks = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "docs/runbooks").glob("m8-*.md"))
    )
    for required in (
        "Ubuntu 26.04",
        "BOOTSTRAP_MAINTENANCE=1",
        "NOT RUN",
        "tudt-restore-*",
        "new volumes",
        "unknown",
        "24-hour soak",
    ):
        assert required in runbooks
    assert "docker compose down -v" not in runbooks
