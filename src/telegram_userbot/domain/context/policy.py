"""Versioned context admission policy and conservative token estimation."""

import math
from dataclasses import dataclass


class ContextAdmissionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    version: str
    max_input_tokens: int = 24_000
    safety_reserve_basis_points: int = 500
    minimum_safety_reserve_tokens: int = 1_024
    current_budget_basis_points: int = 2_000
    recent_budget_basis_points: int = 3_000
    profile_budget_basis_points: int = 1_500
    structured_budget_basis_points: int = 1_500
    semantic_budget_basis_points: int = 1_000
    summary_budget_basis_points: int = 1_000
    structured_limit: int = 12
    semantic_limit: int = 8
    ann_candidate_limit: int = 64
    current_image_limit: int = 10
    fallback_auto_image_tokens: int = 2_048

    def __post_init__(self) -> None:
        allocations = (
            self.current_budget_basis_points,
            self.recent_budget_basis_points,
            self.profile_budget_basis_points,
            self.structured_budget_basis_points,
            self.semantic_budget_basis_points,
            self.summary_budget_basis_points,
        )
        positive = (
            self.max_input_tokens,
            self.minimum_safety_reserve_tokens,
            self.structured_limit,
            self.semantic_limit,
            self.ann_candidate_limit,
            self.current_image_limit,
            self.fallback_auto_image_tokens,
        )
        if not self.version or min(positive) <= 0:
            raise ValueError("context policy values must be positive")
        if sum(allocations) != 10_000 or min(allocations) < 0:
            raise ValueError("context budget allocations must total 10000 basis points")
        if not 0 <= self.safety_reserve_basis_points <= 10_000:
            raise ValueError("context safety reserve is invalid")


@dataclass(frozen=True, slots=True)
class ContextCapabilities:
    max_context_tokens: int
    max_output_tokens: int
    supports_images: bool
    max_images_per_request: int = 10
    auto_image_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_context_tokens <= 0:
            raise ContextAdmissionError("context_capability_unknown")
        if self.max_output_tokens <= 0 or self.max_output_tokens >= self.max_context_tokens:
            raise ContextAdmissionError("context_capability_unknown")
        if self.max_images_per_request <= 0:
            raise ContextAdmissionError("context_image_budget_unknown")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    effective_input_budget: int
    safety_reserve_tokens: int
    image_count_cap: int
    image_token_reserve: int
    section_basis_points: tuple[tuple[str, int], ...]


def calculate_budget(
    policy: ContextPolicy,
    capabilities: ContextCapabilities,
    *,
    required_image_count: int = 0,
) -> BudgetSnapshot:
    if required_image_count < 0:
        raise ContextAdmissionError("context_image_count_invalid")
    safety = max(
        policy.minimum_safety_reserve_tokens,
        math.ceil(capabilities.max_context_tokens * policy.safety_reserve_basis_points / 10_000),
    )
    window_limit = capabilities.max_context_tokens - capabilities.max_output_tokens - safety
    if window_limit <= 0:
        raise ContextAdmissionError("context_capability_unknown")
    if required_image_count and not capabilities.supports_images:
        raise ContextAdmissionError("context_image_count_unsupported")
    image_cap = min(policy.current_image_limit, capabilities.max_images_per_request)
    if required_image_count > image_cap:
        raise ContextAdmissionError("context_image_count_unsupported")
    per_image = capabilities.auto_image_tokens or policy.fallback_auto_image_tokens
    if required_image_count and per_image <= 0:
        raise ContextAdmissionError("context_image_budget_unknown")
    return BudgetSnapshot(
        min(policy.max_input_tokens, window_limit),
        safety,
        image_cap,
        required_image_count * per_image,
        (
            ("current", policy.current_budget_basis_points),
            ("recent", policy.recent_budget_basis_points),
            ("profile", policy.profile_budget_basis_points),
            ("structured", policy.structured_budget_basis_points),
            ("semantic", policy.semantic_budget_basis_points),
            ("summary", policy.summary_budget_basis_points),
        ),
    )


def estimate_utf8_bytes_v1(text: str, *, content_parts: int = 1, messages: int = 1) -> int:
    if content_parts < 0 or messages < 0:
        raise ValueError("token estimator counts cannot be negative")
    text_tokens = math.ceil(len(text.encode()) / 3)
    framed = text_tokens + 8 * content_parts + 12 * messages
    return math.ceil(framed * 1.10)
