"""Exact daily orchestration over source-recomputed slots, without research authorization."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date, timedelta
from pathlib import Path

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.candle_history import ScanCandleHistory, ScanHistoryLineage
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.request_set import DevScanDayEvidence, build_dev_scan_day_evidence
from slagalpha.research.scan_plan import DevScanPlan, build_dev_scan_plan
from slagalpha.research.scan_records import build_source_bound_scan_record
from slagalpha.research.scan_setup import FEATURE_INTERVALS
from slagalpha.research.scan_slot import compute_source_bound_scan_slot
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence
from slagalpha.research.sensitivity import SensitivityPlan, require_dev_execution_inputs
from slagalpha.research.splits import ResearchSplitManifest, audit_research_inputs


def build_source_bound_scan_day(
    *, project_dir: Path, scan_plan: DevScanPlan, selection_date: date,
    sources: Iterable[ScanTradePlanEvidence], split: ResearchSplitManifest,
    plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
    history_lineage: ScanHistoryLineage | None = None,
) -> DevScanDayEvidence:
    """Consume exactly one source per obligation; shared lineage adds cross-day constraints.

    Reusing a lineage never bypasses source revalidation. A failed request-set invocation
    discards its in-memory lineage; there is no approved seed or persisted validation cache.
    """
    scan_plan = DevScanPlan.model_validate(scan_plan.model_dump(mode="json"))
    registry = ContractRegistry.model_validate(registry.model_dump(mode="json"))
    snapshots = tuple(UniverseSnapshot.model_validate(item.model_dump(mode="json"))
                      for item in snapshots)
    current = build_dev_scan_plan(split=split, plan=plan, parameter=parameter, snapshots=snapshots)
    if current != scan_plan:
        raise CandleInputError("daily scan plan does not match current source context")
    audit = audit_research_inputs(split=split, snapshots=snapshots, registry=registry)
    require_dev_execution_inputs(plan, audit)
    day = next((item for item in scan_plan.days if item.selection_date == selection_date), None)
    if day is None:
        raise CandleInputError("source day is not part of the DEV scan plan")
    universe = next(item for item in snapshots if item.selected_at.date() == selection_date)
    lineage = (history_lineage if history_lineage is not None
               else ScanHistoryLineage(scan_plan.plan_hash))
    if lineage.scan_plan_hash != scan_plan.plan_hash:
        raise CandleInputError("daily history lineage belongs to a different scan plan")
    source_iterator = iter(sources)
    exhausted = object()
    records = []
    for index in range(day.time_count):
        at = day.first_confirmation + timedelta(minutes=15 * index)
        for symbol in day.symbols:
            source = next(source_iterator, exhausted)
            if source is exhausted:
                raise CandleInputError("missing daily scan source")
            if not isinstance(source, ScanTradePlanEvidence):
                raise CandleInputError("invalid daily scan source type")
            first = source.trigger_evidence.setup_evidence.features[0]
            history = first.history
            if (history.symbol, history.confirmation_close, history.scan_plan_hash,
                history.universe_content_hash, first.parameter.content_hash) != (
                symbol, at, scan_plan.plan_hash, day.universe_content_hash, parameter.content_hash,
            ):
                raise CandleInputError("daily scan source does not match its exact ordered slot")
            for feature in source.trigger_evidence.setup_evidence.features:
                lineage.require(feature.history)
            # This boundary validates the complete model, rereads ZIP/Parquet and recomputes P3-P6.
            records.append(build_source_bound_scan_record(
                project_dir=project_dir, scan_plan=scan_plan, plan=plan, split=split,
                universe=universe, registry=registry, evidence=source,
            ))
    if next(source_iterator, exhausted) is not exhausted:
        raise CandleInputError("extra daily scan source")
    return build_dev_scan_day_evidence(
        scan_plan=scan_plan, selection_date=selection_date, records=tuple(records),
    )


def require_source_bound_scan_day(
    evidence: DevScanDayEvidence, *, project_dir: Path, scan_plan: DevScanPlan,
    sources: Iterable[ScanTradePlanEvidence], split: ResearchSplitManifest,
    plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
    history_lineage: ScanHistoryLineage | None = None,
) -> None:
    """A saved complete day must still match every recomputed record, including NO_SIGNAL."""
    evidence = DevScanDayEvidence.model_validate(evidence.model_dump(mode="json"))
    if evidence.scan_plan_hash != scan_plan.plan_hash or evidence.day_plan not in scan_plan.days:
        raise CandleInputError("saved source day does not belong to the exact scan plan")
    current = build_source_bound_scan_day(
        project_dir=project_dir, scan_plan=scan_plan,
        selection_date=evidence.day_plan.selection_date, sources=sources,
        split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
        history_lineage=history_lineage,
    )
    if current != evidence:
        raise CandleInputError("saved source day differs from recomputed records")


def compute_source_bound_scan_day(
    *, project_dir: Path, scan_plan: DevScanPlan, selection_date: date,
    history_bundles: Iterable[tuple[ScanCandleHistory, ...]], split: ResearchSplitManifest,
    plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
    history_lineage: ScanHistoryLineage | None = None,
) -> tuple[DevScanDayEvidence, tuple[ScanTradePlanEvidence, ...]]:
    """Compute and revalidate exactly one complete day; return no partial output or write files.

    The caller supplies declared four-timeframe prefixes in time/symbol order. The daily
    gate checks all research context before consuming them. This is not a seed approval,
    downloader, cache, or full-DEV scheduler; retained sources are bounded to this one day.
    """
    lineage = (history_lineage if history_lineage is not None
               else ScanHistoryLineage(scan_plan.plan_hash))
    computed: list[ScanTradePlanEvidence] = []

    def sources() -> Iterator[ScanTradePlanEvidence]:
        # The outer daily builder has validated this exact day and context before iterating.
        day = next(item for item in scan_plan.days if item.selection_date == selection_date)
        bundles = iter(history_bundles)
        exhausted = object()
        for index in range(day.time_count):
            at = day.first_confirmation + timedelta(minutes=15 * index)
            for symbol in day.symbols:
                bundle = next(bundles, exhausted)
                if bundle is exhausted:
                    raise CandleInputError("missing daily scan history bundle")
                if (not isinstance(bundle, tuple) or len(bundle) != 4
                    or any(not isinstance(item, ScanCandleHistory) for item in bundle)):
                    raise CandleInputError("invalid daily scan history bundle")
                if tuple(item.interval for item in bundle) != FEATURE_INTERVALS:
                    raise CandleInputError("daily scan histories must use canonical interval order")
                if any((item.symbol, item.confirmation_close, item.scan_plan_hash,
                        item.universe_content_hash) != (
                            symbol, at, scan_plan.plan_hash, day.universe_content_hash,
                        ) for item in bundle):
                    raise CandleInputError("daily histories do not match their exact ordered slot")
                for history in bundle:
                    lineage.require(history)
                source = compute_source_bound_scan_slot(
                    project_dir=project_dir, scan_plan=scan_plan, histories=bundle,
                    split=split, plan=plan, parameter=parameter,
                    snapshots=snapshots, registry=registry,
                )
                computed.append(source)
                yield source
        if next(bundles, exhausted) is not exhausted:
            raise CandleInputError("extra daily scan history bundle")

    evidence = build_source_bound_scan_day(
        project_dir=project_dir, scan_plan=scan_plan,
        selection_date=selection_date, sources=sources(),
        split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
        history_lineage=lineage,
    )
    return evidence, tuple(computed)
