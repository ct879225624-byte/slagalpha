"""Synthetic closed prefixes: consistency is not seed approval or a raw-validation cache."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.data.klines import normalized_parquet_path
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_history import (
    ScanCandleHistory,
    ScanHistoryLineage,
    load_scan_candle_history,
)
from slagalpha.research.candle_inputs import CandleInputError
from test_candle_history import _history_context, _history_scan_plan
from test_candle_inputs import _partition


def _history_rehash(payload: dict[str, Any]) -> ScanCandleHistory:
    payload = {key: value for key, value in payload.items() if key != "content_hash"}
    return ScanCandleHistory.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


@pytest.fixture
def prefixes(tmp_path: Path) -> tuple[ScanCandleHistory, ScanCandleHistory, ScanCandleHistory]:
    context = _history_context(tmp_path)
    first = load_scan_candle_history(**context)[1]
    context["confirmation_close"] += timedelta(minutes=15)
    extended = load_scan_candle_history(**context)[1]
    context["history_start"] += timedelta(minutes=15)
    reset = load_scan_candle_history(**context)[1]
    return first, extended, reset


def test_continuous_prefix_extension_is_allowed_without_seed_approval(
    prefixes: tuple[ScanCandleHistory, ScanCandleHistory, ScanCandleHistory],
) -> None:
    first, extended, _ = prefixes
    lineage = ScanHistoryLineage(first.scan_plan_hash)
    for history in (first, extended, extended):
        lineage.require(history)
        assert history.history_seed_verified is history.research_authorized is False
    assert extended.row_count == first.row_count + 1


def test_two_individually_valid_raw_archive_versions_cannot_be_mixed(tmp_path: Path) -> None:
    first = load_scan_candle_history(**_history_context(tmp_path / "first"))[1]
    other = _history_context(tmp_path / "other")
    other["sources"] = (_partition(tmp_path / "other", close_prices=("100", "110", "120")),)
    different = load_scan_candle_history(**other)[1]
    assert first.history_start == different.history_start
    assert first.normalized_content_hash != different.normalized_content_hash
    lineage = ScanHistoryLineage(first.scan_plan_hash)
    lineage.require(first)
    with pytest.raises(CandleInputError, match="partition receipt changed"):
        lineage.require(different)


def test_history_lineage_requires_a_real_sha256_plan_reference() -> None:
    with pytest.raises(ValueError):
        ScanHistoryLineage("not-a-plan-hash")


@pytest.mark.parametrize("reverse", [False, True])
def test_individually_source_valid_prefixes_cannot_reset_the_origin(
    prefixes: tuple[ScanCandleHistory, ScanCandleHistory, ScanCandleHistory], reverse: bool,
) -> None:
    first, _, reset = prefixes
    before, after = (reset, first) if reverse else (first, reset)
    lineage = ScanHistoryLineage(first.scan_plan_hash)
    lineage.require(before)
    with pytest.raises(CandleInputError, match="origin changed"):
        lineage.require(after)
    lineage.require(before)  # The failed addition did not replace the original origin.


@pytest.mark.parametrize("field", ["raw", "parquet", "normalized", "receipt_time"])
def test_rehashed_partition_changes_cannot_replace_an_observed_month(
    prefixes: tuple[ScanCandleHistory, ScanCandleHistory, ScanCandleHistory], field: str,
) -> None:
    first, extended, _ = prefixes
    payload = extended.model_dump(mode="json")
    source = payload["sources"][0]
    if field == "raw":
        source["download"]["expected_sha256"] = source["download"]["actual_sha256"] = "0" * 64
        source["normalization"]["source_file_hash"] = "0" * 64
        source["normalization"]["output_relative_path"] = (
            normalized_parquet_path(Path(), extended.sources[0].spec, "0" * 64).as_posix()
        )
    elif field == "parquet":
        source["normalization"]["parquet_sha256"] = "0" * 64
    elif field == "normalized":
        source["normalization"]["normalized_content_hash"] = "0" * 64
    else:
        source["download"]["verified_at"] = "2024-02-29T23:59:59Z"
    changed = _history_rehash(payload)  # Internally valid declaration, not changed raw evidence.
    lineage = ScanHistoryLineage(first.scan_plan_hash)
    lineage.require(first)
    with pytest.raises(CandleInputError, match="partition receipt changed"):
        lineage.require(changed)
    lineage.require(extended)


def test_new_month_extends_but_does_not_replace_old_partition(tmp_path: Path) -> None:
    start = datetime(2023, 12, 31, 23, 30, tzinfo=UTC)
    december = _partition(tmp_path, start=start, rows=2)
    january = _partition(tmp_path)
    context: dict[str, Any] = dict(
                   project_dir=tmp_path, scan_plan=_history_scan_plan(), symbol="BTCUSDT",
                   interval="15m", confirmation_close=datetime(2024, 1, 1, 0, 15, tzinfo=UTC),
                   history_start=start, sources=(december, january))
    full = load_scan_candle_history(**context)[1]
    # A metadata-only earlier prefix ends at midnight (before the first DEV obligation).
    payload = full.model_dump(mode="json")
    payload.update(confirmation_close="2024-01-01T00:00:00Z",
                   last_close_exclusive="2024-01-01T00:00:00Z", row_count=2,
                   sources=[december.model_dump(mode="json")])
    earlier = _history_rehash(payload)
    lineage = ScanHistoryLineage(full.scan_plan_hash)
    lineage.require(earlier)
    lineage.require(full)
    assert len(full.sources) == 2


def test_different_intervals_keep_independent_origins(tmp_path: Path) -> None:
    first = load_scan_candle_history(**_history_context(tmp_path))[1]
    context: dict[str, Any] = dict(
                   project_dir=tmp_path, scan_plan=_history_scan_plan(), symbol="BTCUSDT",
                   interval="1h", confirmation_close=datetime(2024, 1, 1, 2, 15, tzinfo=UTC),
                   history_start=datetime(2024, 1, 1, 1, tzinfo=UTC),
                   sources=(_partition(tmp_path, interval="1h"),))
    hourly = load_scan_candle_history(**context)[1]
    lineage = ScanHistoryLineage(first.scan_plan_hash)
    lineage.require(first)
    lineage.require(hourly)
    lineage.require(first)


@pytest.mark.parametrize("change", ["scan_plan", "invalid_model"])
def test_other_context_and_invalid_models_are_rejected(
    prefixes: tuple[ScanCandleHistory, ScanCandleHistory, ScanCandleHistory], change: str,
) -> None:
    first, _, _ = prefixes
    lineage = ScanHistoryLineage(first.scan_plan_hash)
    payload = first.model_dump(mode="json")
    if change == "scan_plan":
        payload["scan_plan_hash"] = "0" * 64
        changed = _history_rehash(payload)
    else:
        changed = first.model_copy(update={"row_count": first.row_count + 1})
    with pytest.raises(ValueError):
        lineage.require(changed)
    lineage.require(first)
