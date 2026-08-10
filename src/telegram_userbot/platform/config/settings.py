"""M0-safe typed configuration with no credential inputs."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class ConfigurationError(ValueError):
    """A configuration failure whose message never contains the rejected value."""


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


_PREFIX = "TUDT_"
_ALLOWED_KEYS = frozenset(
    {
        "TUDT_ENVIRONMENT",
        "TUDT_INSTANCE_ID",
        "TUDT_LOG_LEVEL",
        "TUDT_ALLOW_NETWORK",
        "TUDT_TELEGRAM_ENABLED",
        "TUDT_PROVIDER_ENABLED",
        "TUDT_DATABASE_ENABLED",
        "TUDT_REDIS_ENABLED",
    }
)
_SENSITIVE_KEY_PARTS = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "API_HASH", "SESSION")
_PLACEHOLDERS = frozenset({"changeme", "change-me", "example", "placeholder", "default"})


def _parse_bool(key: str, raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{key} must be a boolean")


@dataclass(frozen=True, slots=True)
class AppSettings:
    environment: Environment = Environment.DEVELOPMENT
    instance_id: str = "local-m0"
    log_level: str = "INFO"
    allow_network: bool = False
    telegram_enabled: bool = False
    provider_enabled: bool = False
    database_enabled: bool = False
    redis_enabled: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> AppSettings:
        project_keys = {key for key in values if key.startswith(_PREFIX)}
        sensitive_keys = {
            key for key in project_keys if any(part in key.upper() for part in _SENSITIVE_KEY_PARTS)
        }
        if sensitive_keys:
            raise ConfigurationError(
                "credential-like M0 configuration key is forbidden: " + min(sensitive_keys)
            )
        unknown_keys = project_keys - _ALLOWED_KEYS
        if unknown_keys:
            raise ConfigurationError("unknown configuration key: " + min(unknown_keys))

        try:
            environment = Environment(values.get("TUDT_ENVIRONMENT", "development").strip().lower())
        except ValueError as error:
            raise ConfigurationError("TUDT_ENVIRONMENT is invalid") from error

        settings = cls(
            environment=environment,
            instance_id=values.get("TUDT_INSTANCE_ID", "local-m0").strip(),
            log_level=values.get("TUDT_LOG_LEVEL", "INFO").strip().upper(),
            allow_network=_parse_bool(
                "TUDT_ALLOW_NETWORK", values.get("TUDT_ALLOW_NETWORK", "false")
            ),
            telegram_enabled=_parse_bool(
                "TUDT_TELEGRAM_ENABLED", values.get("TUDT_TELEGRAM_ENABLED", "false")
            ),
            provider_enabled=_parse_bool(
                "TUDT_PROVIDER_ENABLED", values.get("TUDT_PROVIDER_ENABLED", "false")
            ),
            database_enabled=_parse_bool(
                "TUDT_DATABASE_ENABLED", values.get("TUDT_DATABASE_ENABLED", "false")
            ),
            redis_enabled=_parse_bool(
                "TUDT_REDIS_ENABLED", values.get("TUDT_REDIS_ENABLED", "false")
            ),
        )
        settings.validate_for_startup()
        return settings

    def validate_for_startup(self) -> None:
        if not self.instance_id or self.instance_id.lower() in _PLACEHOLDERS:
            raise ConfigurationError("TUDT_INSTANCE_ID must be a non-placeholder value")
        if not self.instance_id.replace("-", "").replace("_", "").isalnum():
            raise ConfigurationError("TUDT_INSTANCE_ID contains unsupported characters")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("TUDT_LOG_LEVEL is invalid")
        integrations = (
            self.allow_network,
            self.telegram_enabled,
            self.provider_enabled,
            self.database_enabled,
            self.redis_enabled,
        )
        if any(integrations):
            raise ConfigurationError("M0 startup forbids all external integrations")

    def safe_log_fields(self) -> dict[str, str | bool]:
        return {
            "environment": self.environment.value,
            "instance_id": self.instance_id,
            "network_enabled": self.allow_network,
        }
