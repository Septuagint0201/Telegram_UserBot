"""Canonical model configuration domain."""

from telegram_userbot.domain.model_config.capabilities import (
    ModelCapabilities,
    validate_activation,
)
from telegram_userbot.domain.model_config.config import (
    GENERATION_PROTOCOLS,
    GENERATION_ROLES,
    CanonicalModelConfig,
    LogicalRole,
    ModelConfigurationError,
    ModelProtocol,
    ProfileKind,
    profile_kind_for,
)

__all__ = [
    "GENERATION_PROTOCOLS",
    "GENERATION_ROLES",
    "CanonicalModelConfig",
    "LogicalRole",
    "ModelCapabilities",
    "ModelConfigurationError",
    "ModelProtocol",
    "ProfileKind",
    "profile_kind_for",
    "validate_activation",
]
