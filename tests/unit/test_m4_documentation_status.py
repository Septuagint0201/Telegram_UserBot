from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_m4_status_documents_are_consistent() -> None:
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/Implementation-Plan.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compatibility = (ROOT / "docs/compatibility/m4.md").read_text(encoding="utf-8")

    assert "M4 | Conversation Orchestrator 与 Main AI | REOPENED — CI PENDING" in todo
    assert "- [ ] **M4-011" in todo
    assert "M4 | Conversation Orchestrator与Main AI | fake provider/Telegram | REOPENED" in plan
    assert "因continuation/evidence补强而重开" in readme
    assert "M4当前为`REOPENED — CI PENDING`" in compatibility
    assert "command/outbox→app executor落定" in todo
    assert "Control Bot只写command/outbox" in (ROOT / "docs/Design.md").read_text(encoding="utf-8")
    for document in (todo, plan, readme, compatibility):
        assert "M4已经关闭" not in document
        assert "M4已关闭。" not in document
