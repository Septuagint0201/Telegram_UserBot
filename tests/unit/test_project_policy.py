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
    assert document["project"]["dependencies"] == ["tzdata==2026.3"]
    assert document["project"]["requires-python"] == ">=3.14,<3.15"
