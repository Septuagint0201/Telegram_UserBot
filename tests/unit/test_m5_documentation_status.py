from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_m5_status_documents_are_consistent() -> None:
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/Implementation-Plan.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/Design.md").read_text(encoding="utf-8")
    compatibility = (ROOT / "docs/compatibility/m5.md").read_text(encoding="utf-8")
    disclosure = (ROOT / "DISCLOSURE").read_text(encoding="utf-8")
    assert "- [x] **M5-011" in todo
    assert "M5 | Media 与 Context Contract | COMPLETE" in todo
    assert "M6 | Memory、Summary 与 Embedding Pipeline | IN PROGRESS" in todo
    assert "M5 | Media与Context Contract |" in plan
    assert "| COMPLETE |" in plan
    assert "M0—M5" in readme
    assert "当前进入M6" in readme
    assert "当前进入M6" in design
    assert "0006_m5_media_context" in compatibility
    assert "9e6aeaf3a50ff58826a6830492c766a7983da9b6" in todo
    assert "#2758537825" in todo
    assert "#2758537825" in plan
    assert "#2758537825" in design
    assert "#2758537825" in compatibility
    assert "GitLab pending" not in todo
    assert "pending this stage push" not in disclosure
    assert "Current artifact status: M0-M5 component milestones complete" in disclosure
