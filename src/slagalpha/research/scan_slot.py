"""Compute one source-bound P3-P6 slot after full context checks, without authorizing research."""

from __future__ import annotations

from pathlib import Path

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.candle_history import ScanCandleHistory
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.scan_features import compute_scan_features
from slagalpha.research.scan_plan import DevScanPlan, build_dev_scan_plan
from slagalpha.research.scan_setup import FEATURE_INTERVALS, compute_scan_setup
from slagalpha.research.scan_trade_plan import (
    ScanTradePlanEvidence,
    compute_scan_trade_plan,
    require_active_scan_rule,
)
from slagalpha.research.scan_trigger import compute_scan_trigger
from slagalpha.research.sensitivity import SensitivityPlan, require_dev_execution_inputs
from slagalpha.research.splits import ResearchSplitManifest, audit_research_inputs


def compute_source_bound_scan_slot(
    *, project_dir: Path, scan_plan: DevScanPlan, histories: tuple[ScanCandleHistory, ...],
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
) -> ScanTradePlanEvidence:
    """Reread declared prefixes and compute every stage; no IO until context/bundle checks pass.

    Histories declare their seeds, not seed approval. The caller must separately load those
    receipts; this entry never downloads data, infers missing history, writes output or runs P7.
    """
    scan_plan = DevScanPlan.model_validate(scan_plan.model_dump(mode="json"))
    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    registry = ContractRegistry.model_validate(registry.model_dump(mode="json"))
    snapshots = tuple(UniverseSnapshot.model_validate(item.model_dump(mode="json"))
                      for item in snapshots)
    current = build_dev_scan_plan(split=split, plan=plan, parameter=parameter, snapshots=snapshots)
    if current != scan_plan:
        raise CandleInputError("slot scan plan does not match current source context")
    audit = audit_research_inputs(split=split, snapshots=snapshots, registry=registry)
    require_dev_execution_inputs(plan, audit)
    histories = tuple(ScanCandleHistory.model_validate(item.model_dump(mode="json"))
                      for item in histories)
    if tuple(item.interval for item in histories) != FEATURE_INTERVALS:
        raise CandleInputError("scan slot requires exactly 15m, 1h, 4h, 1d histories in order")
    first = histories[0]
    identities = {(item.scan_plan_hash, item.universe_content_hash, item.symbol,
                   item.confirmation_close) for item in histories}
    if len(identities) != 1:
        raise CandleInputError("all histories must belong to the same scan slot")
    day = next((item for item in scan_plan.days
                if item.first_confirmation <= first.confirmation_close < item.end_exclusive), None)
    if (day is None or first.symbol not in day.symbols
        or first.scan_plan_hash != scan_plan.plan_hash
        or first.universe_content_hash != day.universe_content_hash):
        raise CandleInputError("histories do not match an exact DEV Universe scan slot")
    universe = next(item for item in snapshots if item.selected_at.date() == day.selection_date)
    require_active_scan_rule(registry=registry, symbol=first.symbol, at=first.confirmation_close)
    features = tuple(compute_scan_features(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan,
        parameter=parameter, history=history,
    )[2] for history in histories)
    setup = compute_scan_setup(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, features=features,
    )
    trigger = compute_scan_trigger(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, setup_evidence=setup,
    )
    return compute_scan_trade_plan(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, split=split,
        universe=universe, registry=registry, trigger_evidence=trigger,
    )
