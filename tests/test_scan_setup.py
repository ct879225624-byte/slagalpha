"""Synthetic P4 paths computed from real validators and P3, never hand-supplied indicator values."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.data.archive import archive_path
from slagalpha.data.klines import INTERVAL_MILLISECONDS
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_history import (
    ScanInterval,
    last_closed_boundary,
    load_scan_candle_history,
)
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.scan_features import compute_scan_features
from slagalpha.research.scan_setup import (
    ScanSetupEvidence,
    compute_scan_setup,
    revalidate_scan_setup,
)
from slagalpha.strategy.setup import PullbackState, SetupDecisionReason
from test_candle_history import _history_scan_plan
from test_candle_inputs import _partition
from test_scan_plan import _scan_context


def _setup_context(
    root: Path, *, case: str = "eligible", candidate_index: int = 0,
    at: datetime = datetime(2024, 1, 2, 23, 30, tzinfo=UTC),
) -> dict[str, Any]:
    scan_plan = _history_scan_plan(candidate_index=candidate_index)
    plan = _scan_context()["plan"]
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[candidate_index].candidate_hash,
    )
    features = []
    intervals: tuple[ScanInterval, ...] = ("15m", "1h", "4h", "1d")
    for interval in intervals:
        count = 185 if interval == "4h" and case == "not_ready" else 200
        step = timedelta(milliseconds=INTERVAL_MILLISECONDS[interval])
        end = last_closed_boundary(at, interval)
        start = end - count * step
        prices = [str(100 + index) for index in range(count)]
        if interval == "4h" and case == "neutral":
            prices = ["100"] * count
        if interval == "4h" and case == "compressed":
            prices = [str(100 + index / 1000) for index in range(count)]
        if interval == "1d" and case == "daily_block":
            prices = [str(1000 - index) for index in range(count)]
        if interval == "1h" and case in ("eligible", "trigger", "obstacle"):
            prices[-1] = str(100 + count - 1 - 30)
        overrides = {}
        if interval == "15m" and case in ("trigger", "obstacle"):
            prices = ["100"] * count
            prices[-6] = "99"
            prices[-5:-1] = ["99.5"] * 4
            prices[-1] = "103"
            overrides = {index: {"quote_volume": "800"} for index in range(count - 4, count - 1)}
            overrides[count - 1] = {"high": "103.2", "low": "99", "quote_volume": "1500"}
            if case == "obstacle":
                overrides[count - 20] = {"high": "106"}
        sources = []
        cursor = start
        index = 0
        while cursor < end:
            month_start = cursor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            next_month = (month_start.replace(year=month_start.year + 1, month=1)
                          if month_start.month == 12
                          else month_start.replace(month=month_start.month + 1))
            piece_end = min(next_month, end)
            piece_count = int((piece_end - cursor) / step)
            sources.append(_partition(
                root, start=cursor, interval=interval, rows=piece_count,
                close_prices=tuple(prices[index:index + piece_count]),
                row_overrides={position - index: values for position, values in overrides.items()
                               if index <= position < index + piece_count},
            ))
            cursor = piece_end
            index += piece_count
        _, history = load_scan_candle_history(
            project_dir=root, scan_plan=scan_plan, symbol="BTCUSDT", interval=interval,
            confirmation_close=at, history_start=start, sources=tuple(sources),
        )
        _, _, feature = compute_scan_features(
            project_dir=root, scan_plan=scan_plan, plan=plan, parameter=parameter, history=history,
        )
        features.append(feature)
    return {"project_dir": root, "scan_plan": scan_plan, "plan": plan, "features": tuple(features)}


@pytest.mark.parametrize(("case", "reason"), [
    ("neutral", SetupDecisionReason.FOUR_HOUR_NEUTRAL),
    ("compressed", SetupDecisionReason.FOUR_HOUR_COMPRESSED),
    ("daily_block", SetupDecisionReason.DAILY_BLOCK_LONG),
    ("eligible", SetupDecisionReason.ONE_HOUR_EVALUATED),
])
def test_p4_paths_are_recomputed_from_bound_raw_sources(
    tmp_path: Path, case: str, reason: SetupDecisionReason,
) -> None:
    context = _setup_context(tmp_path, case=case)
    evidence = compute_scan_setup(**context)
    assert evidence.status == "EVALUATED"
    assert evidence.setup is not None
    assert evidence.setup.decision_reason == reason
    if case == "eligible":
        assert evidence.setup.eligible_for_trigger is True
        assert evidence.setup.one_hour is not None
        assert evidence.setup.one_hour.state is PullbackState.STANDARD
    else:
        assert evidence.setup.one_hour is None
    assert evidence.history_seed_verified is evidence.research_authorized is False
    revalidate_scan_setup(
        project_dir=tmp_path, scan_plan=context["scan_plan"],
        plan=context["plan"], evidence=evidence,
    )


def test_185_four_hour_bars_are_not_enough_for_twelve_warmed_ma_orders(tmp_path: Path) -> None:
    context = _setup_context(tmp_path, case="not_ready")
    evidence = compute_scan_setup(**context)
    assert evidence.status == "NOT_READY"
    assert evidence.setup is None
    assert "sma180" in str(evidence.not_ready_detail)


@pytest.mark.parametrize("case", ["missing", "duplicate", "reversed"])
def test_four_timeframe_bundle_cannot_omit_or_repeat_sources(tmp_path: Path, case: str) -> None:
    context = _setup_context(tmp_path)
    features = context["features"]
    context.update(project_dir=tmp_path / "missing", features={
        "missing": features[:-1], "duplicate": (*features[:-1], features[0]),
        "reversed": tuple(reversed(features)),
    }[case])
    with pytest.raises(CandleInputError, match="exactly"):
        compute_scan_setup(**context)


def test_other_candidate_feature_cannot_be_spliced_before_io(tmp_path: Path) -> None:
    context = _setup_context(tmp_path / "first")
    other = _setup_context(tmp_path / "other", candidate_index=1)
    context["features"] = (other["features"][0], *context["features"][1:])
    context["project_dir"] = tmp_path / "missing"
    with pytest.raises(CandleInputError, match="same scan slot"):
        compute_scan_setup(**context)


def test_unchanged_p4_result_does_not_allow_changed_raw_sources(tmp_path: Path) -> None:
    context = _setup_context(tmp_path, case="neutral")
    evidence = compute_scan_setup(**context)
    # Even a short-circuited lower timeframe remains part of the bound source bundle.
    source = context["features"][-1].history.sources[0]
    archive_path(tmp_path / "data/raw", source.spec).write_bytes(b"changed")
    with pytest.raises(CandleInputError, match="raw archive"):
        revalidate_scan_setup(
            project_dir=tmp_path, scan_plan=context["scan_plan"],
            plan=context["plan"], evidence=evidence,
        )


def test_rehashed_setup_output_is_recomputed_not_trusted(tmp_path: Path) -> None:
    context = _setup_context(tmp_path)
    evidence = compute_scan_setup(**context)
    payload = evidence.model_dump(mode="json", exclude={"content_hash"})
    payload["setup"]["eligible_for_trigger"] = False
    forged = ScanSetupEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    with pytest.raises(CandleInputError, match="recomputed source output"):
        revalidate_scan_setup(
            project_dir=tmp_path, scan_plan=context["scan_plan"],
            plan=context["plan"], evidence=forged,
        )


@pytest.mark.parametrize("updates", [
    {"history_seed_verified": True}, {"research_authorized": True},
    {"status": "NOT_READY"}, {"setup": None},
])
def test_setup_evidence_cannot_approve_seeds_or_fabricate_readiness(
    tmp_path: Path, updates: dict[str, Any],
) -> None:
    evidence = compute_scan_setup(**_setup_context(tmp_path))
    with pytest.raises(ValidationError):
        ScanSetupEvidence.model_validate({**evidence.model_dump(mode="json"), **updates})
