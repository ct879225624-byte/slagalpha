"""Raw synthetic P3-P6 lineage; VERIFIED rules here are fixtures, never real historical approval."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.domain.universe import (
    APPROXIMATE_TICK_SIZE_WARNING,
    ContractRegistry,
    EvidenceConfidence,
    RegistryVerification,
)
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.replay_inputs import ReplayDataInputError
from slagalpha.research.scan_trade_plan import (
    ScanTradePlanEvidence,
    compute_scan_trade_plan,
    revalidate_scan_trade_plan,
)
from slagalpha.research.scan_trigger import compute_scan_trigger
from test_historical_replay import _rule
from test_scan_plan import _scan_context
from test_scan_trigger import _trigger_context


def _trade_context(
    root: Path, *, case: str = "trigger", at: datetime = datetime(2024, 1, 2, 12, 30, tzinfo=UTC),
) -> dict[str, Any]:
    context = _trigger_context(root, case=case, at=at)
    trigger = compute_scan_trigger(**context)
    context.pop("setup_evidence")
    scan = _scan_context()
    snapshot = next(item for item in scan["snapshots"]
                    if item.effective_from <= at < item.effective_to)
    universe = snapshot.model_copy(update={
        "members": (snapshot.members[0].model_copy(update={"symbol": "BTCUSDT"}),),
    })
    registry = ContractRegistry(registry_version="synthetic-rules", entries=(
        _rule(RegistryVerification.VERIFIED).model_copy(update={
            "symbol": "BTCUSDT", "base_asset": "BTC",
        }),
    ))
    return {**context, "trigger_evidence": trigger, "universe": universe,
            "registry": registry, "split": scan["split"]}


def test_raw_source_pipeline_builds_accepted_p6_and_bounded_request(tmp_path: Path) -> None:
    context = _trade_context(tmp_path)
    evidence = compute_scan_trade_plan(**context)
    assert evidence.status == "ACCEPTED_PLAN"
    assert evidence.entry_stop is not None and evidence.take_profit is not None
    assert evidence.entry_stop.request.tick_size == Decimal("0.1")
    assert evidence.data_request is not None
    assert evidence.data_request.expected_candle_count == 526
    assert evidence.trigger_evidence.decision is not None
    assert evidence.data_request.request.armed.logical_signal_id == (
        evidence.trigger_evidence.decision.logical_signal_id
    )
    assert evidence.history_seed_verified is evidence.research_authorized is False
    context.pop("trigger_evidence")
    revalidate_scan_trade_plan(evidence=evidence, **context)


@pytest.mark.parametrize("case", ["neutral", "not_ready", "eligible"])
def test_unconfirmed_trigger_does_not_create_price_plan(tmp_path: Path, case: str) -> None:
    evidence = compute_scan_trade_plan(**_trade_context(tmp_path, case=case))
    assert evidence.status == ("NOT_READY" if case == "not_ready" else "SKIPPED_TRIGGER")
    assert evidence.entry_stop is None
    assert evidence.take_profit is None
    assert evidence.data_request is None


@pytest.mark.parametrize("change", ["low_confidence", "inactive", "universe"])
def test_rule_and_universe_mismatch_fail_before_file_access(
    tmp_path: Path, change: str,
) -> None:
    context = _trade_context(tmp_path)
    context["project_dir"] = tmp_path / "missing"
    if change == "universe":
        context["universe"] = context["universe"].model_copy(update={
            "member_count": 0, "members": (),
        })
    else:
        registry = context["registry"]
        update = ({
            "verification_status": RegistryVerification.UNVERIFIED,
            "confidence": EvidenceConfidence.LOW,
        } if change == "low_confidence"
                  else {"effective_to": datetime(2024, 1, 1, tzinfo=UTC)})
        context["registry"] = ContractRegistry(
            registry_version=registry.registry_version,
            entries=(registry.entries[0].model_copy(update=update),),
        )
    with pytest.raises(CandleInputError, match="usable DEV|Universe"):
        compute_scan_trade_plan(**context)


def test_approximate_tick_usage_records_price_and_outcome_impact(tmp_path: Path) -> None:
    context = _trade_context(tmp_path)
    registry = context["registry"]
    context["registry"] = ContractRegistry(
        registry_version=registry.registry_version,
        entries=(registry.entries[0].model_copy(update={
            "verification_status": RegistryVerification.UNVERIFIED,
            "confidence": EvidenceConfidence.MEDIUM,
        }),),
    )

    evidence = compute_scan_trade_plan(**context)

    assert evidence.status == "ACCEPTED_PLAN"
    assert evidence.rule_verification_status is RegistryVerification.UNVERIFIED
    assert evidence.rule_warning_codes == (APPROXIMATE_TICK_SIZE_WARNING,)
    assert evidence.approximate_tick_impact is not None
    assert evidence.approximate_tick_impact.outcome_warning is True
    assert set(evidence.approximate_tick_impact.price_adjustments) == {
        "entry", "stop", "tp1", "tp2",
    }
    assert evidence.approximate_tick_impact.max_adjustment_bps is not None
    assert evidence.data_request is not None
    assert evidence.data_request.rule_warning_codes == (APPROXIMATE_TICK_SIZE_WARNING,)


def test_rule_at_confirmation_is_not_enough_for_full_replay_horizon(tmp_path: Path) -> None:
    context = _trade_context(tmp_path)
    registry = context["registry"]
    context["registry"] = ContractRegistry(registry_version=registry.registry_version, entries=(
        registry.entries[0].model_copy(update={
            "effective_to": datetime(2024, 1, 2, 13, 30, tzinfo=UTC),
        }),
    ))
    with pytest.raises(ReplayDataInputError, match="complete replay window"):
        compute_scan_trade_plan(**context)


def test_p6_near_dev_end_cannot_create_a_cross_split_data_request(tmp_path: Path) -> None:
    context = _trade_context(tmp_path, at=datetime(2024, 1, 2, 23, 30, tzinfo=UTC))
    with pytest.raises(ReplayDataInputError, match="inside DEV"):
        compute_scan_trade_plan(**context)


def test_rehashed_plan_cannot_hide_changed_upstream_binding(tmp_path: Path) -> None:
    context = _trade_context(tmp_path)
    evidence = compute_scan_trade_plan(**context)
    payload = evidence.model_dump(mode="json", exclude={"content_hash"})
    payload["registry_content_hash"] = "0" * 64
    # An internally consistent alteration is still rejected by recomputing the actual registry.
    request = payload["data_request"]
    request["registry_content_hash"] = "0" * 64
    request.pop("request_hash")
    request["request_hash"] = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    forged = ScanTradePlanEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    context.pop("trigger_evidence")
    with pytest.raises(CandleInputError, match="recomputed source output"):
        revalidate_scan_trade_plan(evidence=forged, **context)


def test_entry_stop_rejection_never_requests_market_data(tmp_path: Path) -> None:
    context = _trade_context(tmp_path)
    registry = context["registry"]
    context["registry"] = ContractRegistry(registry_version=registry.registry_version, entries=(
        registry.entries[0].model_copy(update={"tick_size": Decimal("10")}),
    ))
    evidence = compute_scan_trade_plan(**context)
    assert evidence.status == "REJECTED_ENTRY_STOP"
    assert evidence.data_request is None
    assert evidence.take_profit is None


def test_nearby_obstacle_rejects_tp_without_requesting_market_data(tmp_path: Path) -> None:
    evidence = compute_scan_trade_plan(**_trade_context(tmp_path, case="obstacle"))
    assert evidence.status == "REJECTED_TAKE_PROFIT"
    assert evidence.entry_stop is not None and evidence.entry_stop.accepted
    assert evidence.take_profit is not None and not evidence.take_profit.accepted
    assert evidence.data_request is None


@pytest.mark.parametrize("updates", [
    {"research_authorized": True}, {"history_seed_verified": True},
    {"data_request": None}, {"status": "SKIPPED_TRIGGER"},
])
def test_price_plan_cannot_claim_readiness_or_authority(
    tmp_path: Path, updates: dict[str, Any],
) -> None:
    evidence = compute_scan_trade_plan(**_trade_context(tmp_path))
    with pytest.raises(ValidationError):
        ScanTradePlanEvidence.model_validate({**evidence.model_dump(mode="json"), **updates})
