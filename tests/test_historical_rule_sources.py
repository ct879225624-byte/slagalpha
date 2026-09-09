"""Synthetic source audits prove point observations never become rule intervals."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slagalpha.data.historical_rule_sources import (
    ArchivedExchangeInfoSource,
    HistoricalRuleSourceAudit,
    HistoricalRuleSourceError,
    audit_archived_exchange_info,
    write_historical_rule_source_audit,
)
from test_exchange_info import exchange_payload


def _source(raw: bytes) -> ArchivedExchangeInfoSource:
    return ArchivedExchangeInfoSource(
        replay_url="https://web.archive.org/web/20240201000001id_/https://fapi.binance.com/fapi/v1/exchangeInfo",
        archive_capture_at=datetime(2024, 2, 1, 0, 0, 1, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 9, tzinfo=UTC),
        cdx_digest="SYNTHETIC_TEST_DIGEST",
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_point_snapshot_is_extracted_but_never_grants_interval_coverage(tmp_path: Path) -> None:
    raw = exchange_payload()
    report = audit_archived_exchange_info(
        raw,
        source=_source(raw),
        dev_rule_gap_hash="a" * 64,
        target_symbols=("BTCUSDT", "MISSINGUSDT"),
        priority_symbols=("BTCUSDT",),
    )
    assert report.status == "BLOCKED"
    assert report.eligible_member_day_count == 0
    assert report.observed_target_symbols == ("BTCUSDT",)
    assert report.missing_target_symbols == ("MISSINGUSDT",)
    assert report.priority_observations[0].tick_size.as_tuple().exponent == -2
    assert report.blockers == (
        "ARCHIVE_SOURCE_AUTHENTICITY_REVIEW_REQUIRED",
        "DEV_TARGETS_MISSING_FROM_SNAPSHOT",
        "POINT_IN_TIME_SNAPSHOT_HAS_NO_INTERVAL_CONTINUITY",
    )
    assert report.registry_modified is report.research_authorized is False
    output = write_historical_rule_source_audit(report, tmp_path)
    assert HistoricalRuleSourceAudit.model_validate_json(output.read_bytes()) == report
    assert write_historical_rule_source_audit(report, tmp_path) == output


def test_unrelated_unparseable_contract_is_reported_without_losing_target() -> None:
    payload = json.loads(exchange_payload())
    invalid = dict(payload["symbols"][0])
    invalid["symbol"] = invalid["pair"] = "PENDINGUSDT"
    invalid["contractType"] = ""
    payload["symbols"].append(invalid)
    raw = json.dumps(payload).encode()
    report = audit_archived_exchange_info(
        raw,
        source=_source(raw),
        dev_rule_gap_hash="b" * 64,
        target_symbols=("BTCUSDT",),
        priority_symbols=("BTCUSDT",),
    )
    assert report.observed_target_symbols == ("BTCUSDT",)
    assert report.unparseable_snapshot_symbols == ("PENDINGUSDT",)
    assert "SNAPSHOT_CONTAINS_UNPARSEABLE_CONTRACTS" in report.blockers


@pytest.mark.parametrize("change", ["hash", "duplicate", "future-server", "duplicate-target"])
def test_tampered_or_ambiguous_sources_are_rejected(change: str) -> None:
    raw = exchange_payload()
    source = _source(raw)
    targets: tuple[str, ...] = ("BTCUSDT",)
    if change == "hash":
        raw = raw + b" "
    elif change == "duplicate":
        raw = raw.replace(b'"serverTime":', b'"serverTime":1,"serverTime":', 1)
        source = _source(raw)
    elif change == "future-server":
        payload = json.loads(raw)
        payload["serverTime"] = int(source.archive_capture_at.timestamp() * 1000) + 1
        raw = json.dumps(payload).encode()
        source = _source(raw)
    else:
        targets = ("TESTUSDT", "TESTUSDT")
    with pytest.raises(HistoricalRuleSourceError):
        audit_archived_exchange_info(
            raw,
            source=source,
            dev_rule_gap_hash="c" * 64,
            target_symbols=targets,
            priority_symbols=("BTCUSDT",),
        )


def test_report_hash_and_safety_flags_cannot_be_changed() -> None:
    raw = exchange_payload()
    report = audit_archived_exchange_info(
        raw,
        source=_source(raw),
        dev_rule_gap_hash="d" * 64,
        target_symbols=("BTCUSDT",),
        priority_symbols=("BTCUSDT",),
    )
    for update in (
        {"research_authorized": True},
        {"eligible_member_day_count": 1},
        {"report_hash": "f" * 64},
    ):
        with pytest.raises(ValueError):
            HistoricalRuleSourceAudit.model_validate({**report.model_dump(), **update})
