"""Compact DEV source scan report invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest
from pydantic import ValidationError

from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.dev_source_scan import (
    AcceptedP6Request,
    DevScanFunnel,
    _aligned_stream_start,
    _close_times_ns,
    _IncrementalZoneState,
    _merged_windows,
    _periods,
    _PreparedStream,
    _read_symbol_checkpoint,
    _visible_end,
    _write_symbol_checkpoint,
)
from slagalpha.strategy.pivots import PivotType, merge_pivot_zones
from test_pivots import make_pivot


def test_periods_are_monthly_and_inclusive() -> None:
    assert _periods("2023-11", "2024-02") == (
        "2023-11",
        "2023-12",
        "2024-01",
        "2024-02",
    )


def test_stream_origin_ceil_aligns_lifecycle_to_each_timeframe() -> None:
    lifecycle = datetime(2023, 3, 30, 12, 30, tzinfo=UTC)
    assert _aligned_stream_start(lifecycle, "15m") == lifecycle
    assert _aligned_stream_start(lifecycle, "1h") == datetime(
        2023, 3, 30, 13, tzinfo=UTC
    )
    assert _aligned_stream_start(lifecycle, "4h") == datetime(
        2023, 3, 30, 16, tzinfo=UTC
    )
    assert _aligned_stream_start(lifecycle, "1d") == datetime(
        2023, 3, 31, tzinfo=UTC
    )


def test_visible_end_compares_nanoseconds_for_millisecond_arrow_timestamps() -> None:
    at = datetime(2025, 1, 29, 20, 15, tzinfo=UTC)
    closes = pd.Series(pd.DatetimeIndex([at]).as_unit("ms"))
    assert _close_times_ns(closes) == (int(at.timestamp() * 1_000_000_000),)
    stream = cast(
        _PreparedStream,
        SimpleNamespace(
            close_ns=(
                int((at - timedelta(minutes=15)).timestamp() * 1_000_000_000),
                int(at.timestamp() * 1_000_000_000),
                int((at + timedelta(minutes=15)).timestamp() * 1_000_000_000),
            )
        ),
    )
    assert _visible_end(stream, at) == 2


def test_market_data_windows_merge_only_within_symbol() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)

    def request(symbol: str, offset: int, duration: int) -> SimpleNamespace:
        return SimpleNamespace(request=SimpleNamespace(
            request=SimpleNamespace(armed=SimpleNamespace(symbol=symbol)),
            start=start + timedelta(minutes=offset),
            end_exclusive=start + timedelta(minutes=offset + duration),
        ))

    requests = cast(
        tuple[AcceptedP6Request, ...],
        (
            request("BTCUSDT", 0, 60),
            request("BTCUSDT", 30, 90),
            request("ETHUSDT", 0, 30),
        ),
    )
    assert _merged_windows(requests) == (2, 150)


def test_funnel_rejects_unreconciled_counts() -> None:
    values = {field: 0 for field in DevScanFunnel.model_fields}
    values["scan_slot_count"] = 1
    with pytest.raises(ValidationError, match="scan slots do not reconcile"):
        DevScanFunnel.model_validate(values)


def test_incremental_zones_match_each_full_prefix() -> None:
    pivots = (
        make_pivot(1, 100.0),
        make_pivot(2, 101.9),
        make_pivot(3, 100.5, kind=PivotType.HIGH),
        make_pivot(4, 105.0),
    )
    state = _IncrementalZoneState()
    for count in range(1, len(pivots) + 1):
        assert state.available(
            pivots, count, datetime(2024, 1, 1, tzinfo=UTC)
        ) == merge_pivot_zones(pivots[:count])


def test_symbol_checkpoint_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    path = tmp_path / "symbol.json"
    binding_hash = "a" * 64
    payload = {
        "schema_version": "dev-source-symbol-scan/0.1.0",
        "binding_hash": binding_hash,
        "symbol": "BTCUSDT",
    }
    _write_symbol_checkpoint(path, payload)
    assert _read_symbol_checkpoint(
        path, binding_hash=binding_hash, symbol="BTCUSDT"
    ) == payload

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(CandleInputError, match="invalid DEV source scan checkpoint"):
        _read_symbol_checkpoint(
            path, binding_hash=binding_hash, symbol="BTCUSDT"
        )
