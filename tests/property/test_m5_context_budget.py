from datetime import UTC, datetime
from uuid import uuid7

import pytest
from hypothesis import given
from hypothesis import strategies as st

from telegram_userbot.domain.context import (
    Candidate,
    ContextAdmissionError,
    ContextCapabilities,
    ContextLayer,
    ContextPolicy,
    ContextSource,
    TrustLevel,
    build_context,
    calculate_budget,
)
from telegram_userbot.domain.shared.redaction import SensitiveValue


@pytest.mark.property
@given(
    max_context=st.integers(min_value=3_000, max_value=200_000),
    max_output=st.integers(min_value=1, max_value=2_000),
    configured=st.integers(min_value=1, max_value=100_000),
)
def test_effective_context_budget_never_exceeds_policy_or_model_window(
    max_context: int, max_output: int, configured: int
) -> None:
    if max_output + 1_024 >= max_context:
        return
    policy = ContextPolicy("property-v1", max_input_tokens=configured)
    snapshot = calculate_budget(
        policy,
        ContextCapabilities(max_context, max_output, False),
    )
    assert snapshot.effective_input_budget <= configured
    assert (
        snapshot.effective_input_budget + max_output + snapshot.safety_reserve_tokens <= max_context
    )


@pytest.mark.property
@given(
    text=st.text(min_size=1, max_size=500), token_estimate=st.integers(min_value=1, max_value=300)
)
def test_current_turn_is_never_partially_truncated(text: str, token_estimate: int) -> None:
    candidate = Candidate(
        uuid7(),
        "revision-1",
        "current:1",
        ContextLayer.CURRENT,
        datetime(2030, 1, 1, tzinfo=UTC),
        token_estimate,
    )
    source = ContextSource(
        candidate,
        "user",
        "contact",
        TrustLevel.UNTRUSTED_USER,
        SensitiveValue(text),
        "message_revision",
    )
    policy = ContextPolicy("property-v1", max_input_tokens=240)
    budget = calculate_budget(policy, ContextCapabilities(5_000, 100, False))
    if token_estimate <= budget.effective_input_budget:
        built = build_context(
            manifest_id=uuid7(),
            purpose="reactive_reply",
            logical_role="main_ai",
            sources=(source,),
            budget=budget,
            builder_version="context-builder-v1",
            prompt_version="prompt-v1",
            prompt_bundle_sha256="e" * 64,
            context_policy_version="context-v1",
            retrieval_policy_version="retrieval-v1",
            capability_snapshot_sha256="a" * 64,
        )
        assert built.ordered_sources == (source,)
    else:
        with pytest.raises(ContextAdmissionError, match="current_turn_over_budget"):
            build_context(
                manifest_id=uuid7(),
                purpose="reactive_reply",
                logical_role="main_ai",
                sources=(source,),
                budget=budget,
                builder_version="context-builder-v1",
                prompt_version="prompt-v1",
                prompt_bundle_sha256="e" * 64,
                context_policy_version="context-v1",
                retrieval_policy_version="retrieval-v1",
                capability_snapshot_sha256="a" * 64,
            )
