"""Tests for one-record-per-line structured logging."""

from __future__ import annotations

import json
import logging
from io import StringIO
from typing import cast

from slagalpha.config import LogLevel
from slagalpha.logging_config import configure_logging


def test_configure_logging_emits_json_with_event_fields() -> None:
    stream = StringIO()
    configure_logging(LogLevel.INFO, stream=stream)

    logging.getLogger("slagalpha.test").info(
        "configuration is valid",
        extra={"event": "configuration_valid", "symbol": "BTCUSDT"},
    )

    payload = cast(dict[str, object], json.loads(stream.getvalue()))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "slagalpha.test"
    assert payload["message"] == "configuration is valid"
    assert payload["event"] == "configuration_valid"
    assert payload["symbol"] == "BTCUSDT"
    assert str(payload["timestamp"]).endswith("+00:00")


def test_configure_logging_replaces_existing_handlers() -> None:
    root_logger = logging.getLogger()
    root_logger.addHandler(logging.NullHandler())

    configure_logging("WARNING", stream=StringIO())

    assert len(root_logger.handlers) == 1
    assert isinstance(root_logger.handlers[0], logging.StreamHandler)
    assert root_logger.level == logging.WARNING

