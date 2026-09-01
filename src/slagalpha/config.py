"""Application configuration with an explicit, secret-free V0.1 surface."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator


class Environment(StrEnum):
    """Supported runtime environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    """Validated standard-library log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseModel):
    """Validated settings used by the current offline engineering skeleton."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_name: str = "slagalpha"
    environment: Environment = Environment.DEVELOPMENT
    log_level: LogLevel = LogLevel.INFO
    data_dir: Path = Path("data")
    artifacts_dir: Path = Path("artifacts")

    @field_validator("data_dir", "artifacts_dir", mode="before")
    @classmethod
    def validate_path(cls, value: Any) -> Any:
        """Reject blank paths instead of silently treating them as the working directory."""

        if isinstance(value, str) and not value.strip():
            raise ValueError("path must not be blank")
        return value

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Load the small allow-listed environment surface.

        Account credentials and API keys are intentionally absent in V0.1.
        """

        source = os.environ if environ is None else environ
        field_map = {
            "SLAGALPHA_ENVIRONMENT": "environment",
            "SLAGALPHA_LOG_LEVEL": "log_level",
            "SLAGALPHA_DATA_DIR": "data_dir",
            "SLAGALPHA_ARTIFACTS_DIR": "artifacts_dir",
        }
        values = {
            field_name: source[environment_name]
            for environment_name, field_name in field_map.items()
            if environment_name in source
        }
        return cls.model_validate(values)

    def public_dict(self) -> dict[str, str]:
        """Return the complete public configuration in a stable display form."""

        return {
            "app_name": self.app_name,
            "environment": self.environment.value,
            "log_level": self.log_level.value,
            "data_dir": str(self.data_dir),
            "artifacts_dir": str(self.artifacts_dir),
        }

