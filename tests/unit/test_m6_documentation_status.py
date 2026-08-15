from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_m6_status_documents_are_consistent() -> None:
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/Implementation-Plan.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/Design.md").read_text(encoding="utf-8")
    compatibility = (ROOT / "docs/compatibility/m6.md").read_text(encoding="utf-8")
    disclosure = (ROOT / "DISCLOSURE").read_text(encoding="utf-8")
    assert "- [x] **M6-012" in todo
    assert "M6 | Memory、Summary 与 Embedding Pipeline | COMPLETE" in todo
    assert (
        "M6 | Memory/Summary/Embedding Pipeline | fake provider/embedding | "
        "COMPLETE — WINDOWS/GITLAB LINUX PASS"
    ) in plan
    assert "M0—M6" in readme
    assert "M0—M6" in design
    assert "0007_m6_memory_pipeline" in compatibility
    assert "45秒quiet OR 20 revisions OR 6000 tokens OR 10分钟" in compatibility
    assert "真实backup/restore" in todo
    assert "`NOT RUN`" in compatibility
    assert "Current artifact status: M0-M6 complete" in disclosure
    assert "no memory pipeline" not in disclosure
    for document in (todo, plan, readme, design, compatibility, disclosure):
        assert "9be7012edf3aabe1dd5db7a325b0f36efce27063" in document
        assert "2762159878" in document
