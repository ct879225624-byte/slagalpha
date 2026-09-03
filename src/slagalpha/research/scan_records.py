"""Convert source-recomputed slot results into declared records, never research approval."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.request_set import DevScanRecord
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence, revalidate_scan_trade_plan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest


def build_source_bound_scan_record(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    split: ResearchSplitManifest, universe: UniverseSnapshot, registry: ContractRegistry,
    evidence: ScanTradePlanEvidence,
) -> DevScanRecord:
    """Recheck raw sources and all stages before mapping a slot; source errors propagate."""
    evidence = ScanTradePlanEvidence.model_validate(evidence.model_dump(mode="json"))
    revalidate_scan_trade_plan(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, split=split,
        universe=universe, registry=registry, evidence=evidence,
    )
    trigger = evidence.trigger_evidence
    setup = trigger.setup_evidence
    history = setup.features[0].history
    outcome: Literal["NO_SIGNAL", "REJECTED_PLAN", "ACCEPTED_PLAN", "BLOCKED"]
    reasons = {f"P6:{evidence.status}"}
    if evidence.status == "NOT_READY":
        outcome = "BLOCKED"
        reasons.add("P4:NOT_READY" if setup.status == "NOT_READY" else "P5:NOT_READY")
    elif evidence.status == "SKIPPED_TRIGGER":
        outcome = "NO_SIGNAL"
        if trigger.status == "SKIPPED_SETUP":
            reasons.add("P5:SKIPPED_SETUP")
            assert setup.setup is not None
            reasons.add(f"P4:{setup.setup.decision_reason.value}")
            if setup.setup.one_hour is not None:
                reasons.update(f"P4:1H:{code.value}" for code in setup.setup.one_hour.reason_codes)
        else:
            reasons.add("P5:NO_CONFIRMED_TRIGGER")
            assert trigger.decision is not None
            reasons.update(f"P5:A:{code.value}" for code in trigger.decision.trigger_a.reason_codes)
            if trigger.decision.trigger_b is not None:
                reasons.update(f"P5:B:{code.value}"
                               for code in trigger.decision.trigger_b.reason_codes)
    else:
        outcome = "ACCEPTED_PLAN" if evidence.status == "ACCEPTED_PLAN" else "REJECTED_PLAN"
        assert evidence.entry_stop is not None
        reasons.update(f"P6:ENTRY_STOP:{code.value}" for code in evidence.entry_stop.reason_codes)
        if evidence.take_profit is not None:
            reasons.update(f"P6:TAKE_PROFIT:{code.value}"
                           for code in evidence.take_profit.reason_codes)
    return DevScanRecord(
        symbol=history.symbol, confirmation_close=history.confirmation_close, outcome=outcome,
        reason_codes=tuple(sorted(reasons)), upstream_evidence_hash=evidence.content_hash,
        trade_plan=evidence.take_profit if outcome == "ACCEPTED_PLAN" else None,
    )


def require_source_bound_scan_record(
    record: DevScanRecord, *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    split: ResearchSplitManifest, universe: UniverseSnapshot, registry: ContractRegistry,
    evidence: ScanTradePlanEvidence,
) -> None:
    """An old record or its upstream hash is not a substitute for current source revalidation."""
    record = DevScanRecord.model_validate(record.model_dump(mode="json"))
    observed = build_source_bound_scan_record(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, split=split,
        universe=universe, registry=registry, evidence=evidence,
    )
    if observed != record:
        raise CandleInputError("scan record does not match the recomputed source evidence")
