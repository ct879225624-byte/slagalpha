"""Recomputed synthetic P3 evidence with explicit unapproved starting history."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from slagalpha.data.archive import archive_path
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_history import load_scan_candle_history
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.scan_features import (
    ScanFeatureEvidence,
    compute_scan_features,
    indicator_frame_hash,
    revalidate_scan_features,
)
from slagalpha.strategy.indicators import compute_indicator_frame
from test_candle_history import _history_scan_plan
from test_candle_inputs import _partition
from test_scan_plan import _scan_context


def _feature_context(
    root: Path, *, candidate_index: int = 0, future_changed: bool = False,
) -> dict[str, Any]:
    prices = tuple(str(100 + index % 8) for index in range(200))
    prices = ("500", *prices[1:190], *("900" for _ in range(10))) if future_changed else (
        "500", *prices[1:],
    )
    source = _partition(root, rows=200, close_prices=prices)
    scan_plan = _history_scan_plan(candidate_index=candidate_index)
    _, history = load_scan_candle_history(
        project_dir=root, scan_plan=scan_plan, symbol="BTCUSDT", interval="15m",
        confirmation_close=datetime(2024, 1, 2, 23, 30, tzinfo=UTC),
        history_start=datetime(2024, 1, 1, tzinfo=UTC), sources=(source,),
    )
    plan = _scan_context()["plan"]
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[candidate_index].candidate_hash,
    )
    return {"project_dir": root, "scan_plan": scan_plan, "plan": plan,
            "parameter": parameter, "history": history}


def test_feature_evidence_is_recomputed_and_retains_unapproved_seed(tmp_path: Path) -> None:
    context = _feature_context(tmp_path)
    candles, indicators, evidence = compute_scan_features(**context)
    assert len(candles) == len(indicators) == 190
    assert evidence.pivots and evidence.zones
    assert indicators["sma180"].isna().sum() == 179
    assert indicators["atr14"].isna().sum() == 13
    assert evidence.indicator_content_hash == indicator_frame_hash(indicators)
    assert evidence.history_seed_verified is evidence.strategy_executed is False
    assert evidence.research_authorized is False
    restored = ScanFeatureEvidence.model_validate_json(evidence.model_dump_json())
    again_candles, again_indicators = revalidate_scan_features(
        project_dir=tmp_path, scan_plan=context["scan_plan"],
        plan=context["plan"], evidence=restored,
    )
    pd.testing.assert_frame_equal(candles, again_candles)
    pd.testing.assert_frame_equal(indicators, again_indicators)


def test_future_rows_do_not_change_past_indicators_or_confirmed_structures(tmp_path: Path) -> None:
    _, first, evidence = compute_scan_features(**_feature_context(tmp_path / "first"))
    _, second, changed = compute_scan_features(**_feature_context(
        tmp_path / "second", future_changed=True,
    ))
    pd.testing.assert_frame_equal(first, second)
    assert evidence.indicator_content_hash == changed.indicator_content_hash
    assert evidence.pivots == changed.pivots
    assert evidence.zones == changed.zones
    # Whole monthly source identity changes even though the causally visible result does not.
    assert evidence.history.content_hash != changed.history.content_hash


def test_full_prefix_does_not_silently_reseed_at_185_bars(tmp_path: Path) -> None:
    candles, indicators, _ = compute_scan_features(**_feature_context(tmp_path))
    incorrectly_reseeded = compute_indicator_frame(candles.iloc[-185:].reset_index(drop=True))
    assert indicators["atr14"].iloc[-1] != incorrectly_reseeded["atr14"].iloc[-1]


@pytest.mark.parametrize("candidate_index", range(10))
def test_exact_candidate_pivot_window_is_used(tmp_path: Path, candidate_index: int) -> None:
    context = _feature_context(tmp_path, candidate_index=candidate_index)
    _, _, evidence = compute_scan_features(**context)
    assert evidence.pivots
    window = context["parameter"].candidate.parameters.pivot_window
    assert all((pivot.left, pivot.right) == window for pivot in evidence.pivots)
    assert all(pivot.confirmed_at <= evidence.history.confirmation_close
               for pivot in evidence.pivots)


def test_blocked_plan_or_mixed_candidate_rejected_before_file_io(tmp_path: Path) -> None:
    context = _feature_context(tmp_path)
    plan = context["plan"]
    context["parameter"] = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[1].candidate_hash,
    )
    context["project_dir"] = tmp_path / "missing"
    with pytest.raises(CandleInputError, match="same scan candidate"):
        compute_scan_features(**context)
    blocked = _scan_context(ready=False)
    context.update(plan=blocked["plan"], parameter=blocked["parameter"])
    with pytest.raises(CandleInputError, match="blocked"):
        compute_scan_features(**context)


@pytest.mark.parametrize("case", ["hash", "omit_pivots"])
def test_self_consistent_saved_output_is_recomputed_not_trusted(tmp_path: Path, case: str) -> None:
    context = _feature_context(tmp_path)
    _, _, evidence = compute_scan_features(**context)
    payload = evidence.model_dump(mode="json", exclude={"content_hash"})
    if case == "hash":
        payload["indicator_content_hash"] = "0" * 64
    else:
        payload.update(pivots=[], zones=[])
    forged = ScanFeatureEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    with pytest.raises(CandleInputError, match="recomputed source output"):
        revalidate_scan_features(
            project_dir=tmp_path, scan_plan=context["scan_plan"],
            plan=context["plan"], evidence=forged,
        )


def test_changed_raw_bytes_invalidate_saved_features(tmp_path: Path) -> None:
    context = _feature_context(tmp_path)
    _, _, evidence = compute_scan_features(**context)
    archive_path(tmp_path / "data/raw", context["history"].sources[0].spec).write_bytes(b"changed")
    with pytest.raises(CandleInputError, match="raw archive"):
        revalidate_scan_features(
            project_dir=tmp_path, scan_plan=context["scan_plan"],
            plan=context["plan"], evidence=evidence,
        )


@pytest.mark.parametrize("updates", [
    {"research_authorized": True}, {"history_seed_verified": True},
    {"strategy_executed": True}, {"indicator_content_hash": "not-a-hash"},
])
def test_feature_evidence_never_grants_research_or_seed_authority(
    tmp_path: Path, updates: dict[str, Any],
) -> None:
    _, _, evidence = compute_scan_features(**_feature_context(tmp_path))
    with pytest.raises(ValidationError):
        ScanFeatureEvidence.model_validate({**evidence.model_dump(mode="json"), **updates})


def test_indicator_hash_keeps_nulls_and_exact_float_identity() -> None:
    frame = pd.DataFrame({"test": [float("nan"), 0.0, -0.0, 0.1]})
    assert indicator_frame_hash(frame) == indicator_frame_hash(frame.copy())
    changed = frame.copy()
    changed.loc[1, "test"] = -0.0
    assert indicator_frame_hash(frame) != indicator_frame_hash(changed)
    with pytest.raises(CandleInputError, match="infinity"):
        indicator_frame_hash(pd.DataFrame({"test": [float("inf")]}))
