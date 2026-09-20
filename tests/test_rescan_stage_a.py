"""Phase 3 Stage A executor boundaries; no real candidate scan is run here."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from slagalpha.research import dev_source_scan as scanner
from slagalpha.research.candle_inputs import CandleInputError, CandlePartitionSource
from slagalpha.research.dev_source_scan import (
    ConfirmedTriggerLedger,
    DevScanFunnel,
    DevSourceScanReport,
    ScanExecutionBinding,
    _prepare_stream,
    _PreparedStream,
    _setup_at,
    validate_execution_checkpoint,
)
from slagalpha.research.rescan_stage_a import (
    FINAL_LEDGER_HASH,
    baseline_equivalence_check,
    evaluate_scale_thresholds,
    source_code_hash,
    threshold_policy,
)
from slagalpha.strategy.indicators import compute_indicator_frame
from slagalpha.strategy.setup import Direction, FourHourEvaluation, MaOrder, Regime


def _binding() -> ScanExecutionBinding:
    return ScanExecutionBinding(
        executor_version="p9-rescan-stage-a/0.1.0",
        scanner_algorithm_version="dev-source-scan-algorithm/0.1.1",
        candidate_hash="1" * 64,
        parameter_content_hash="2" * 64,
        sensitivity_plan_hash="3" * 64,
        scan_plan_hash="4" * 64,
        split_hash="5" * 64,
        registry_hash="6" * 64,
        source_inventory_hash="7" * 64,
        universe_hash="8" * 64,
        source_code_hash="9" * 64,
        environment_lock_hash="a" * 64,
        threshold_policy_hash="b" * 64,
        source_catalog_hashes={"BTCUSDT": "c" * 64},
    )


def _checkpoint(binding: ScanExecutionBinding) -> dict[str, Any]:
    funnel = {field: 0 for field in DevScanFunnel.model_fields}
    return {
        "schema_version": "dev-source-symbol-scan/0.1.0",
        "binding_hash": "d" * 64,
        "symbol": "BTCUSDT",
        "symbol_slot_count": 0,
        "outcome_counts": {},
        "reason_counts": {},
        "accepted_requests": [],
        "confirmed_triggers": [],
        "scanned": True,
        "anomaly": None,
        "validated_source_partition_count": 0,
        "source_partition_count": 0,
        "fallback_rule_slot_count": 0,
        "fallback_price_plan_count": 0,
        "approximate_price_observation_count": 0,
        "approximate_outcome_warning_count": 0,
        "max_approximate_tick_adjustment_bps": None,
        "execution_binding": binding.model_dump(mode="json"),
        "diagnostics": {
            "funnel": funnel,
            "pivot_counts": {"15m": 0, "1h": 0},
            "zone_observation_counts": {"15m": 0, "1h": 0},
            "structure_observation_slots": 0,
        },
    }


def _candles(interval: str, rows: int, minutes: int) -> pd.DataFrame:
    start = datetime(2023, 2, 1, tzinfo=UTC)
    opened = pd.date_range(start, periods=rows, freq=f"{minutes}min")
    close = np.linspace(100.0, 101.0, rows)
    return pd.DataFrame({
        "open_time": opened,
        "close_time_exclusive": opened + pd.Timedelta(minutes=minutes),
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "quote_volume": np.full(rows, 1000.0),
        "is_closed": True,
        "symbol": "BTCUSDT",
        "interval": interval,
    })


def test_compression_threshold_is_explicitly_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = _candles("4h", 200, 240)
    indicators = compute_indicator_frame(candles)
    stream = cast(_PreparedStream, SimpleNamespace(candles=candles, indicators=indicators))
    observed: list[float] = []
    def wrapped(*args: Any, compression_threshold: float) -> Any:
        observed.append(compression_threshold)
        return FourHourEvaluation(
            direction=Direction.NEUTRAL,
            current_order=MaOrder.MIXED_ORDER,
            regime=Regime.TRENDING,
            band_width=1.0,
            sma180_change_5=0.0,
            order_change_count=0,
            band_flip_count=0,
            compression_threshold=compression_threshold,
            compression_reasons=(),
        )

    monkeypatch.setattr(scanner, "evaluate_four_hour", wrapped)
    result = _setup_at(
        cast(dict[Any, _PreparedStream], {"4h": stream}),
        cast(dict[Any, int], {"4h": len(candles)}),
        compression_threshold=0.50,
    )
    assert observed == [0.50]
    assert result.four_hour.compression_threshold == 0.50


def test_pivot_window_uses_existing_detector_path(monkeypatch: pytest.MonkeyPatch) -> None:
    candles = _candles("15m", 30, 15)
    observed: list[tuple[int, int]] = []
    source = cast(CandlePartitionSource, SimpleNamespace(
        spec=SimpleNamespace(interval="15m"),
        normalization=SimpleNamespace(first_open_time=candles["open_time"].iloc[0]),
    ))
    monkeypatch.setattr(scanner, "_load_bound_candle_partition", lambda *_: candles)

    def detector(*args: Any, left: int, right: int) -> tuple[Any, ...]:
        observed.append((left, right))
        return ()

    monkeypatch.setattr(scanner, "detect_confirmed_pivots", detector)
    _prepare_stream(
        Path("."), (source,), end_exclusive=candles["close_time_exclusive"].iloc[-1],
        lifecycle_start=candles["open_time"].iloc[0], pivot_window=(3, 3),
    )
    assert observed == [(3, 3)]


def test_source_anomaly_stops_checkpoint_recovery() -> None:
    binding = _binding()
    checkpoint = _checkpoint(binding)
    checkpoint.update({"scanned": False, "anomaly": "source changed"})
    with pytest.raises(CandleInputError, match="reconciliation"):
        validate_execution_checkpoint(checkpoint, binding)


def test_checkpoint_source_code_hash_invalidation() -> None:
    binding = _binding()
    checkpoint = _checkpoint(binding)
    changed = binding.model_copy(update={"source_code_hash": "e" * 64})
    with pytest.raises(CandleInputError, match="binding mismatch"):
        validate_execution_checkpoint(checkpoint, changed)


def test_candidate_binding_mismatch_is_rejected() -> None:
    binding = _binding()
    checkpoint = _checkpoint(binding)
    changed = binding.model_copy(update={"candidate_hash": "f" * 64})
    with pytest.raises(CandleInputError, match="binding mismatch"):
        validate_execution_checkpoint(checkpoint, changed)


def test_stage_a_support_data_is_deterministic() -> None:
    root = Path(__file__).resolve().parents[1]
    assert source_code_hash(root) == source_code_hash(root)
    assert threshold_policy() == threshold_policy()
    assert threshold_policy()["out_of_band_action"] == "MANUAL_REVIEW_AND_FAIL_CLOSED"


def test_deterministic_checkpoint_rerun() -> None:
    binding = _binding()
    checkpoint = _checkpoint(binding)
    first = validate_execution_checkpoint(checkpoint, binding)
    second = validate_execution_checkpoint(checkpoint, binding)
    assert first == second


def test_scale_thresholds_are_fixed_before_candidate_execution() -> None:
    root = Path(__file__).resolve().parents[1]
    report = DevSourceScanReport.model_validate_json(
        (root / "data" / "manifests" / "dev_source_scan"
         / "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2.json"
         ).read_bytes()
    )
    evaluation = evaluate_scale_thresholds(report)
    assert evaluation["status"] == "PASS"
    assert all(evaluation["within_band"].values())


def test_checkpoint_with_complete_zero_funnel_reconciles() -> None:
    binding = _binding()
    assert validate_execution_checkpoint(_checkpoint(binding), binding).scan_slot_count == 0


def test_baseline_equivalence_against_frozen_report_and_final_ledger() -> None:
    root = Path(__file__).resolve().parents[1]
    manifests = root / "data" / "manifests"
    report = DevSourceScanReport.model_validate_json(
        (manifests / "dev_source_scan"
         / "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2.json"
         ).read_bytes()
    )
    ledger = ConfirmedTriggerLedger.model_validate_json(
        (manifests / "confirmed_trigger_ledger" / f"{FINAL_LEDGER_HASH}.json").read_bytes()
    )
    assert baseline_equivalence_check(
        report=report,
        trusted_report=report,
        records=ledger.records,
        final_ledger=ledger,
    )
