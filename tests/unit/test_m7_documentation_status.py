from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_m7_status_documents_are_consistent() -> None:
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/Implementation-Plan.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/Design.md").read_text(encoding="utf-8")
    compatibility = (ROOT / "docs/compatibility/m7.md").read_text(encoding="utf-8")
    disclosure = (ROOT / "DISCLOSURE").read_text(encoding="utf-8")

    assert "M7-001" in todo
    assert "0010_account_scope_refs" in compatibility
    assert "M7" in plan
    assert "M7" in readme
    assert "M7" in design
    assert "Proactive Pipeline" in disclosure
    assert "真实 Telegram/provider" in compatibility
    assert "`NOT RUN`" in compatibility
    for document in (todo, plan, readme, design, compatibility, disclosure):
        assert "22:00" in document
        assert "00:00" in document
