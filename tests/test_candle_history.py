"""Synthetic closed-prefix boundaries; a chosen prefix is not an approved historical ATR seed."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from slagalpha.data.archive import archive_path
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_history import (
    ScanCandleHistory,
    load_scan_candle_history,
    reload_scan_candle_history,
)
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.scan_plan import DevScanPlan, build_dev_scan_plan
from test_candle_inputs import _partition
from test_scan_plan import _scan_context


def _history_scan_plan(*, candidate_index: int = 0) -> DevScanPlan:
    context = _scan_context()
    context["snapshots"] = tuple(snapshot.model_copy(update={
        "members": (snapshot.members[0].model_copy(update={"symbol": "BTCUSDT"}),),
    }) for snapshot in context["snapshots"])
    context["parameter"] = build_dev_parameter_version(
        plan=context["plan"],
        candidate_hash=context["plan"].candidates[candidate_index].candidate_hash,
    )
    return build_dev_scan_plan(**context)


def _history_context(root: Path) -> dict[str, Any]:
    return {
        "project_dir": root, "scan_plan": _history_scan_plan(), "symbol": "BTCUSDT",
        "interval": "15m", "confirmation_close": datetime(2024, 1, 1, 0, 30, tzinfo=UTC),
        "history_start": datetime(2024, 1, 1, tzinfo=UTC), "sources": (_partition(root),),
    }


@pytest.mark.parametrize(("interval", "at", "expected"), [
    ("15m", datetime(2024, 1, 1, 0, 30, tzinfo=UTC), 2),
    ("1h", datetime(2024, 1, 1, 2, 15, tzinfo=UTC), 2),
    ("4h", datetime(2024, 1, 1, 8, 15, tzinfo=UTC), 2),
    ("1d", datetime(2024, 1, 2, 0, 15, tzinfo=UTC), 1),
])
def test_only_fully_closed_candles_are_visible_at_scan_time(
    tmp_path: Path, interval: str, at: datetime, expected: int,
) -> None:
    context = _history_context(tmp_path)
    context.update(interval=interval, confirmation_close=at, sources=(
        context["sources"][0] if interval == "15m" else _partition(tmp_path, interval=interval),
    ))
    frame, receipt = load_scan_candle_history(**context)
    assert len(frame) == receipt.row_count == expected
    assert bool((frame["close_time_exclusive"] <= at).all())
    assert frame["close_time_exclusive"].iloc[-1] == receipt.last_close_exclusive
    assert receipt.history_seed_verified is receipt.research_authorized is False
    restored = ScanCandleHistory.model_validate_json(receipt.model_dump_json())
    pd.testing.assert_frame_equal(frame, reload_scan_candle_history(
        project_dir=tmp_path, scan_plan=context["scan_plan"], history=restored,
    ))


def test_cross_month_prefix_keeps_both_sources_and_pre_dev_history(tmp_path: Path) -> None:
    context = _history_context(tmp_path)
    start = datetime(2023, 12, 31, 23, 30, tzinfo=UTC)
    older = _partition(tmp_path, start=start, rows=2)
    context.update(history_start=start, sources=(older, *context["sources"]))
    frame, receipt = load_scan_candle_history(**context)
    assert len(frame) == 4
    assert frame["open_time"].iloc[0] == start
    assert len(receipt.sources) == 2
    assert receipt.history_seed_verified is False


@pytest.mark.parametrize("case", ["missing_month", "duplicate", "reversed", "extra"])
def test_month_coverage_rejected_before_reading_sources(tmp_path: Path, case: str) -> None:
    context = _history_context(tmp_path)
    start = datetime(2023, 12, 31, 23, 30, tzinfo=UTC)
    older = _partition(tmp_path, start=start, rows=2)
    current = context["sources"][0]
    context.update(history_start=start, project_dir=tmp_path / "missing", sources={
        "missing_month": (current,), "duplicate": (older, older),
        "reversed": (current, older), "extra": (older, current, current),
    }[case])
    with pytest.raises(ValidationError, match="canonical months"):
        load_scan_candle_history(**context)


@pytest.mark.parametrize("case", ["leading", "trailing", "month_seam"])
def test_missing_bars_are_not_filled_or_treated_as_no_signal(
    tmp_path: Path, case: str,
) -> None:
    context = _history_context(tmp_path)
    if case == "leading":
        context["sources"] = (_partition(tmp_path, start=datetime(2024, 1, 1, 0, 15, tzinfo=UTC)),)
    elif case == "trailing":
        context["sources"] = (_partition(tmp_path, rows=1),)
    else:
        start = datetime(2023, 12, 31, 23, 30, tzinfo=UTC)
        context.update(history_start=start, sources=(_partition(tmp_path, start=start, rows=1),
                                                    *context["sources"]))
    with pytest.raises(CandleInputError, match="missing, duplicate"):
        load_scan_candle_history(**context)


@pytest.mark.parametrize("updates", [
    {"symbol": "ETHUSDT"}, {"confirmation_close": datetime(2024, 1, 3, 0, 15, tzinfo=UTC)},
    {"confirmation_close": datetime(2024, 1, 1, 0, 31, tzinfo=UTC)},
    {"confirmation_close": datetime(2024, 1, 1, 0, 30)},
    {"history_start": datetime(2024, 1, 1, 0, 1, tzinfo=UTC)},
    {"history_start": datetime(2024, 1, 1, 0, 30, tzinfo=UTC)},
])
def test_invalid_scope_is_rejected_before_file_io(tmp_path: Path, updates: dict[str, Any]) -> None:
    context = _history_context(tmp_path)
    context.update(updates)
    context["project_dir"] = tmp_path / "missing"
    with pytest.raises(ValueError, match="slot|UTC|grid|greater than"):
        load_scan_candle_history(**context)


def test_saved_history_cannot_rebind_to_other_candidate_or_changed_raw(tmp_path: Path) -> None:
    context = _history_context(tmp_path)
    _, receipt = load_scan_candle_history(**context)
    with pytest.raises(CandleInputError, match="no longer matches"):
        reload_scan_candle_history(
            project_dir=tmp_path / "missing", scan_plan=_history_scan_plan(candidate_index=1),
            history=receipt,
        )
    archive_path(tmp_path / "data/raw", receipt.sources[0].spec).write_bytes(b"changed")
    with pytest.raises(CandleInputError, match="raw archive"):
        reload_scan_candle_history(
            project_dir=tmp_path, scan_plan=context["scan_plan"], history=receipt,
        )


def test_same_count_rehashed_history_cannot_change_visible_content(tmp_path: Path) -> None:
    context = _history_context(tmp_path)
    _, receipt = load_scan_candle_history(**context)
    payload = receipt.model_dump(mode="json", exclude={"content_hash"})
    payload["normalized_content_hash"] = "0" * 64
    changed = ScanCandleHistory.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    with pytest.raises(CandleInputError, match="no longer matches"):
        reload_scan_candle_history(
            project_dir=tmp_path, scan_plan=context["scan_plan"], history=changed,
        )


@pytest.mark.parametrize("updates", [
    {"history_seed_verified": True}, {"research_authorized": True}, {"row_count": 1},
    {"last_close_exclusive": "2024-01-01T00:45:00Z"},
])
def test_history_receipt_cannot_expand_visibility_or_approve_seed(
    tmp_path: Path, updates: dict[str, Any],
) -> None:
    _, receipt = load_scan_candle_history(**_history_context(tmp_path))
    with pytest.raises(ValidationError):
        ScanCandleHistory.model_validate({**receipt.model_dump(mode="json"), **updates})


def test_midnight_uses_previous_universe_and_does_not_require_new_month(tmp_path: Path) -> None:
    context = _history_context(tmp_path)
    # Scan midnight remains in the prior effective Universe day; only already-closed bars count.
    at = datetime(2024, 1, 2, tzinfo=UTC)
    context.update(confirmation_close=at, history_start=at - timedelta(minutes=30),
                   sources=(_partition(tmp_path, start=at - timedelta(minutes=30), rows=2),))
    _, receipt = load_scan_candle_history(**context)
    assert receipt.universe_content_hash == context["scan_plan"].days[0].universe_content_hash
