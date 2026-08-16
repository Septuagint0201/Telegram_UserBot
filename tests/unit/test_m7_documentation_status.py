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
    assert "0018_m5_retry_budget_proof" in compatibility
    assert "0019_m5_m6_recovery_execution" in compatibility
    assert "0020_m5_m7_review_hardening" in compatibility
    assert "0021_m7_evidence_activity" in compatibility
    assert "M7" in plan
    assert "M7" in readme
    assert "M7" in design
    assert "Proactive Pipeline" in disclosure
    assert "真实 Telegram/provider" in compatibility
    assert "`NOT RUN`" in compatibility
    for document in (todo, plan, readme, compatibility):
        assert "ff8d32a6d9998d4e242ab2c5d407511c4e1cf6bd" in document
        assert "31952960402" in document
        assert "a55afa1d6df1337f14b7ce56d4a3eb5bdf59319a7feb20716560c8b301a81508" in document
    for document in (todo, plan, readme, design, compatibility, disclosure):
        assert "22:00" in document
        assert "00:00" in document
