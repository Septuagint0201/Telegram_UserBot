from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMPLEMENTATION_SOURCE = "19bf0c7974b7ef2e1a3e3b8064a10d4d162353b6"
IMPLEMENTATION_RUN = "32229187875"
FINAL_ACCEPTANCE = "7af2f524fcc4fc30fc04aa40de88a7b1302eb526"
FINAL_TREE = "a2eaab5195c393c3905cc92620af54b7d8c208ab"
FINAL_RUN = "32234678340"


def _assert_shared_m7_status(document: str) -> None:
    assert IMPLEMENTATION_SOURCE in document
    assert IMPLEMENTATION_RUN in document
    assert FINAL_ACCEPTANCE in document
    assert FINAL_TREE in document
    assert FINAL_RUN in document
    assert "NOT RUN" in document
    assert "COMPAT-LINUX-ARM64-001" in document
    assert "Linux amd64" in document
    assert "不会重绑" not in document


def _assert_shared_m7_contract(document: str) -> None:
    assert "0022_m7_job_scope_and_deadline" in document
    assert "0023_m7_proactive_snapshot" in document
    assert "0024_runtime_fencing_provenance" in document
    assert "22:00" in document
    assert "00:00" in document


def _assert_m7_compatibility_evidence(compatibility: str) -> None:
    assert "0018_m5_retry_budget_proof" in compatibility
    assert "0019_m5_m6_recovery_execution" in compatibility
    assert "0020_m5_m7_review_hardening" in compatibility
    assert "0021_m7_evidence_activity" in compatibility
    assert "0022_m7_job_scope_and_deadline" in compatibility
    assert "0023_m7_proactive_snapshot" in compatibility
    assert "0024_runtime_fencing_provenance" in compatibility
    assert "真实 Telegram/provider" in compatibility
    assert "`NOT RUN`" in compatibility
    assert "422 passed" in compatibility
    assert "58 deselected" in compatibility
    assert "479 passed" in compatibility
    assert "1 deselected" in compatibility
    assert "Chromium browser contract为1 passed" in compatibility
    assert "M7 acceptance为12/12" in compatibility
    assert "96 tables" in compatibility
    assert "0 unnamed constraints" in compatibility
    assert "5 migration paths" in compatibility
    assert "locked-install" in compatibility
    assert "`FAIL`" in compatibility
    assert "不属于M7或M8的`linux/amd64`门禁" in compatibility


@pytest.mark.unit
def test_m7_status_documents_are_consistent() -> None:
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/Implementation-Plan.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/Design.md").read_text(encoding="utf-8")
    compatibility = (ROOT / "docs/compatibility/m7.md").read_text(encoding="utf-8")
    disclosure = (ROOT / "DISCLOSURE").read_text(encoding="utf-8")
    documents = (todo, plan, readme, design, compatibility, disclosure)

    assert "M7-001" in todo
    assert "M7" in plan
    assert "M7" in readme
    assert "M7" in design
    assert "Proactive Pipeline" in disclosure
    _assert_m7_compatibility_evidence(compatibility)
    for document in documents:
        _assert_shared_m7_status(document)
        _assert_shared_m7_contract(document)
