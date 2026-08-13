from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_m4_status_documents_are_consistent() -> None:
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/Implementation-Plan.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compatibility = (ROOT / "docs/compatibility/m4.md").read_text(encoding="utf-8")

    assert "M4 | Conversation Orchestrator 与 Main AI | COMPLETE" in todo
    assert "- [x] **M4-011" in todo
    assert "M5 | Media 与 Context Contract | COMPLETE" in todo
    assert "M4 | Conversation Orchestrator与Main AI | fake provider/Telegram | COMPLETE" in plan
    assert "M5 | Media与Context Contract |" in plan
    assert "| COMPLETE |" in plan
    assert "V1架构设计与M0—M5已经完成" in readme
    assert "M4当前为`COMPLETE — WINDOWS/GITLAB LINUX PASS`" in compatibility
    assert "2758187631" in todo
    assert "2758187631" in plan
    assert "2758187631" in readme
    assert "2758187631" in compatibility
    assert "Control Bot只写command/outbox" in (ROOT / "docs/Design.md").read_text(encoding="utf-8")
    for document in (todo, plan, readme, compatibility):
        assert "REOPENED — CI PENDING" not in document
        assert "BLOCKED BY M4" not in document
