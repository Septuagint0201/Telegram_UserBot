import tomllib
from pathlib import Path

import pytest
from scripts.check_import_boundaries import boundary_violations

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_import_boundaries_are_clean() -> None:
    assert boundary_violations(ROOT / "src" / "telegram_userbot") == ()


@pytest.mark.unit
def test_pytest_policy_is_strict_and_deselects_sensitive_suites() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = document["tool"]["pytest"]["ini_options"]
    addopts = options["addopts"]
    assert "--strict-markers" in addopts
    marker_expression = addopts[addopts.index("-m") + 1]
    for marker in ("integration", "recovery", "soak", "live", "requires_secret"):
        assert f"not {marker}" in marker_expression


@pytest.mark.unit
def test_runtime_dependencies_and_python_are_pinned() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["dependencies"] == [
        "SQLAlchemy==2.0.51",
        "alembic==1.19.1",
        "arq==0.28.0",
        "cryptography==50.0.0",
        "httpx==0.28.1",
        "psycopg[binary]==3.3.4",
        "redis==5.3.1",
        "starlette==1.6.0",
        "tzdata==2026.3",
        "uvicorn==0.52.1",
    ]
    assert document["project"]["requires-python"] == ">=3.14,<3.15"


@pytest.mark.unit
def test_windows_only_lock_dependency_keeps_its_platform_marker() -> None:
    lock = (ROOT / "requirements" / "dev.lock").read_text(encoding="utf-8")
    assert 'pywin32==312 ; sys_platform == "win32" \\' in lock


@pytest.mark.unit
def test_session_scoped_integration_engine_has_matching_test_loops() -> None:
    conftest = (ROOT / "tests" / "integration" / "conftest.py").read_text(encoding="utf-8")
    assert '@pytest_asyncio.fixture(scope="session", loop_scope="session")' in conftest
    for path in sorted((ROOT / "tests" / "integration").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        assert 'pytestmark = pytest.mark.asyncio(loop_scope="session")' in source
