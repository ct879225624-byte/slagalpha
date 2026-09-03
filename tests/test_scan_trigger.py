"""Synthetic P4/P5 provenance tests using raw monthly source fixtures throughout."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.scan_features import revalidate_scan_features
from slagalpha.research.scan_setup import compute_scan_setup
from slagalpha.research.scan_trigger import (
    ScanTriggerEvidence,
    compute_scan_trigger,
    revalidate_scan_trigger,
)
from slagalpha.strategy.triggers import TriggerType
from test_scan_setup import _setup_context


def _trigger_context(root: Path, *, case: str = "trigger") -> dict[str, Any]:
    context = _setup_context(root, case=case)
    setup = compute_scan_setup(**context)
    context.pop("features")
    return {**context, "setup_evidence": setup}


def test_ma_reclaim_recomputed_from_raw_p4_p3_and_frozen_hourly_region(tmp_path: Path) -> None:
    context = _trigger_context(tmp_path)
    evidence = compute_scan_trigger(**context)
    assert evidence.status == "EVALUATED"
    assert evidence.decision is not None
    assert evidence.decision.eligible_for_plan is True
    assert evidence.decision.primary_trigger is TriggerType.MA_RECLAIM
    assert evidence.trigger_context is not None
    _, hourly = revalidate_scan_features(
        project_dir=tmp_path, scan_plan=context["scan_plan"], plan=context["plan"],
        evidence=context["setup_evidence"].features[1],
    )
    assert (evidence.trigger_context.region_lower, evidence.trigger_context.region_upper) == (
        min(hourly["sma60"].iloc[-1], hourly["sma90"].iloc[-1]),
        max(hourly["sma60"].iloc[-1], hourly["sma90"].iloc[-1]),
    )
    assert evidence.history_seed_verified is evidence.research_authorized is False
    revalidate_scan_trigger(
        project_dir=tmp_path, scan_plan=context["scan_plan"],
        plan=context["plan"], evidence=evidence,
    )


@pytest.mark.parametrize(("case", "status"), [
    ("neutral", "SKIPPED_SETUP"), ("not_ready", "NOT_READY"), ("eligible", "EVALUATED"),
])
def test_no_setup_insufficient_history_and_failed_trigger_remain_distinct(
    tmp_path: Path, case: str, status: str,
) -> None:
    evidence = compute_scan_trigger(**_trigger_context(tmp_path, case=case))
    assert evidence.status == status
    if case == "eligible":
        assert evidence.decision is not None
        assert evidence.decision.eligible_for_plan is False
    else:
        assert evidence.decision is None
        assert evidence.trigger_context is None


@pytest.mark.parametrize("case", ["region", "decision"])
def test_forged_trigger_output_or_hand_entered_region_cannot_survive_recomputation(
    tmp_path: Path, case: str,
) -> None:
    context = _trigger_context(tmp_path)
    evidence = compute_scan_trigger(**context)
    payload = evidence.model_dump(mode="json", exclude={"content_hash"})
    if case == "region":
        payload["trigger_context"]["region_lower"] -= 1
    else:
        payload["decision"]["eligible_for_plan"] = False
    forged = ScanTriggerEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    with pytest.raises(CandleInputError, match="recomputed source output"):
        revalidate_scan_trigger(
            project_dir=tmp_path, scan_plan=context["scan_plan"],
            plan=context["plan"], evidence=forged,
        )


@pytest.mark.parametrize("updates", [
    {"history_seed_verified": True}, {"research_authorized": True},
    {"status": "SKIPPED_SETUP"}, {"decision": None},
])
def test_trigger_evidence_rejects_fabricated_authority_and_status(
    tmp_path: Path, updates: dict[str, Any],
) -> None:
    evidence = compute_scan_trigger(**_trigger_context(tmp_path))
    with pytest.raises(ValidationError):
        ScanTriggerEvidence.model_validate({**evidence.model_dump(mode="json"), **updates})
