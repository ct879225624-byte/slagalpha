"""Tests for the intentionally small P1 CLI surface."""

from __future__ import annotations

import json
from typing import cast

import pytest

from slagalpha.cli import main


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert (
        "{version,config-check,archive-ingest,archive-plan,archive-capacity,"
        "archive-download-plan,archive-normalize-plan}"
        in capsys.readouterr().out
    )


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_config_check_prints_public_configuration(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLAGALPHA_ENVIRONMENT", "test")
    monkeypatch.setenv("SLAGALPHA_LOG_LEVEL", "WARNING")

    assert main(["config-check"]) == 0

    captured = capsys.readouterr()
    payload = cast(dict[str, object], json.loads(captured.out))
    assert payload["environment"] == "test"
    assert payload["log_level"] == "WARNING"
    assert "api_key" not in captured.out.lower()


def test_config_check_reports_validation_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLAGALPHA_LOG_LEVEL", "VERBOSE")

    assert main(["config-check"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration invalid" in captured.err


def test_archive_ingest_rejects_invalid_month_before_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "archive-ingest",
                "--symbol",
                "BTCUSDT",
                "--interval",
                "15m",
                "--year",
                "2024",
                "--month",
                "13",
            ]
        )
        == 1
    )
    assert "archive ingestion failed" in capsys.readouterr().err


def test_archive_plan_rejects_invalid_month_before_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "archive-plan",
                "--symbol",
                "ANTUSDT",
                "--interval",
                "15m",
                "--start",
                "2024-3",
                "--end-exclusive",
                "2024-04",
            ]
        )
    assert error.value.code == 2
    assert "zero-padded YYYY-MM" in capsys.readouterr().err
