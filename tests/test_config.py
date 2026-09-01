"""Tests for the allow-listed V0.1 configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.config import Environment, LogLevel, Settings


def test_defaults_are_offline_and_secret_free() -> None:
    settings = Settings.from_env({})

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.log_level is LogLevel.INFO
    assert settings.data_dir == Path("data")
    assert settings.artifacts_dir == Path("artifacts")
    assert set(settings.public_dict()) == {
        "app_name",
        "environment",
        "log_level",
        "data_dir",
        "artifacts_dir",
    }


def test_allow_list_environment_overrides() -> None:
    settings = Settings.from_env(
        {
            "SLAGALPHA_ENVIRONMENT": "test",
            "SLAGALPHA_LOG_LEVEL": "DEBUG",
            "SLAGALPHA_DATA_DIR": "test-data",
            "SLAGALPHA_ARTIFACTS_DIR": "test-artifacts",
            "UNRELATED_VALUE": "ignored",
        }
    )

    assert settings.environment is Environment.TEST
    assert settings.log_level is LogLevel.DEBUG
    assert settings.data_dir == Path("test-data")
    assert settings.artifacts_dir == Path("test-artifacts")


@pytest.mark.parametrize(
    ("environment", "expected_fragment"),
    [
        ({"SLAGALPHA_LOG_LEVEL": "VERBOSE"}, "log_level"),
        ({"SLAGALPHA_ENVIRONMENT": "paper"}, "environment"),
        ({"SLAGALPHA_DATA_DIR": "   "}, "path must not be blank"),
    ],
)
def test_invalid_configuration_fails_closed(
    environment: dict[str, str], expected_fragment: str
) -> None:
    with pytest.raises(ValidationError, match=expected_fragment):
        Settings.from_env(environment)


def test_settings_are_immutable() -> None:
    settings = Settings()

    with pytest.raises(ValidationError, match="frozen"):
        settings.log_level = LogLevel.ERROR

