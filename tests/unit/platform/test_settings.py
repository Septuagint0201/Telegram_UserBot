import pytest

from telegram_userbot.platform.config.settings import AppSettings, ConfigurationError, Environment


@pytest.mark.unit
def test_default_settings_are_local_and_network_disabled() -> None:
    settings = AppSettings.from_mapping({})
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.safe_log_fields() == {
        "environment": "development",
        "instance_id": "local-m0",
        "network_enabled": False,
    }


@pytest.mark.unit
def test_explicit_safe_settings_parse() -> None:
    settings = AppSettings.from_mapping(
        {
            "TUDT_ENVIRONMENT": "test",
            "TUDT_INSTANCE_ID": "test-01",
            "TUDT_LOG_LEVEL": "warning",
            "TUDT_ALLOW_NETWORK": "off",
        }
    )
    assert settings.environment is Environment.TEST
    assert settings.log_level == "WARNING"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TUDT_ALLOW_NETWORK", "true"),
        ("TUDT_TELEGRAM_ENABLED", "1"),
        ("TUDT_PROVIDER_ENABLED", "yes"),
        ("TUDT_DATABASE_ENABLED", "on"),
        ("TUDT_REDIS_ENABLED", "true"),
    ],
)
def test_m0_rejects_every_external_integration(key: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match="external integrations"):
        AppSettings.from_mapping({key: value})


@pytest.mark.unit
def test_credential_like_key_is_rejected_without_echoing_value() -> None:
    private_value = "SYNTHETIC_CREDENTIAL_VALUE"
    with pytest.raises(ConfigurationError) as error:
        AppSettings.from_mapping({"TUDT_API_KEY": private_value})
    assert "TUDT_API_KEY" in str(error.value)
    assert private_value not in str(error.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "values",
    [
        {"TUDT_UNKNOWN": "x"},
        {"TUDT_ENVIRONMENT": "unknown"},
        {"TUDT_ALLOW_NETWORK": "maybe"},
        {"TUDT_INSTANCE_ID": "changeme"},
        {"TUDT_INSTANCE_ID": "bad space"},
        {"TUDT_LOG_LEVEL": "TRACE"},
    ],
)
def test_invalid_configuration_fails_closed(values: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        AppSettings.from_mapping(values)
