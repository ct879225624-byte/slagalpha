"""Compute or recover complete DEV requests with daily raw-source revalidation."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path

from pydantic import TypeAdapter

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.candle_history import ScanCandleHistory, ScanHistoryLineage
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.request_set import (
    DevReplayRequestSet,
    DevScanDayEvidence,
    build_dev_replay_request_set,
)
from slagalpha.research.request_set_market_data import (
    DevRequestMarketDataPair,
    DevRequestSetMarketDataReport,
    require_request_set_market_data_report_binding,
    verify_dev_request_set_market_data,
)
from slagalpha.research.scan_days import compute_source_bound_scan_day
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.scan_storage import (
    restore_source_bound_scan_day,
    save_source_bound_scan_day,
)
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest


def compute_source_bound_request_set_with_checkpoints(
    *, project_dir: Path,
    history_days: Iterable[tuple[date, Iterable[tuple[ScanCandleHistory, ...]]]],
    scan_plan: DevScanPlan, split: ResearchSplitManifest, plan: SensitivityPlan,
    parameter: DevParameterVersion, snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> DevReplayRequestSet:
    """Audit, compute and save each exact day, then return the complete reconciled request set.

    Failure may leave valid daily checkpoints, never a partial request set. Retrying recomputes
    all days and reuses only identical files; no automatic resume or persisted approval exists.
    History streams are supplied by the caller. This writes daily sources but never downloads,
    saves the returned request set, approves seeds, or runs P7. Only current-day P6 is retained.
    """
    def computed_days() -> Iterator[DevScanDayEvidence]:
        # The outer request builder audits the entire frozen context before starting this body.
        lineage = ScanHistoryLineage(scan_plan.plan_hash)
        inputs = iter(history_days)
        exhausted = object()
        for expected in scan_plan.days:
            item = next(inputs, exhausted)
            if item is exhausted:
                raise CandleInputError("missing DEV history day")
            if not isinstance(item, tuple) or len(item) != 2:
                raise CandleInputError("invalid DEV history day input")
            selection_date, bundles = item
            if selection_date != expected.selection_date:
                raise CandleInputError("history day does not match its exact ordered DEV position")
            evidence, sources = compute_source_bound_scan_day(
                project_dir=project_dir, scan_plan=scan_plan, selection_date=selection_date,
                history_bundles=bundles, split=split, plan=plan, parameter=parameter,
                snapshots=snapshots, registry=registry, history_lineage=lineage,
            )
            save_source_bound_scan_day(
                evidence, project_dir=project_dir, sources=sources, scan_plan=scan_plan,
                split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
            )
            yield evidence
            del evidence, sources
        if next(inputs, exhausted) is not exhausted:
            raise CandleInputError("extra DEV history day")

    return build_dev_replay_request_set(
        scan_plan=scan_plan, evidence=computed_days(), split=split, plan=plan, parameter=parameter,
        snapshots=snapshots, registry=registry,
    )


def _restored_scan_days(
    *, project_dir: Path, day_hashes: tuple[str, ...], scan_plan: DevScanPlan,
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
) -> Iterator[DevScanDayEvidence]:
    hashes = TypeAdapter(tuple[Sha256, ...]).validate_python(day_hashes)
    if len(hashes) != len(scan_plan.days) or len(set(hashes)) != len(hashes):
        raise CandleInputError("source day hashes must cover the complete DEV plan without repeats")
    # Fresh per invocation, shared by all recovered days, including those without requests.
    lineage = ScanHistoryLineage(scan_plan.plan_hash)
    for expected, digest in zip(scan_plan.days, hashes, strict=True):
        day = restore_source_bound_scan_day(
            project_dir=project_dir, content_hash=digest, scan_plan=scan_plan,
            split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
            history_lineage=lineage,
        )
        if (day.content_hash != digest or day.scan_plan_hash != scan_plan.plan_hash
            or day.day_plan != expected):
            raise CandleInputError("restored source day differs from the exact ordered DEV plan")
        yield day


def build_source_bound_replay_request_set(
    *, project_dir: Path, day_hashes: tuple[str, ...], scan_plan: DevScanPlan,
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
) -> DevReplayRequestSet:
    """Audit first, recompute each recovered day, then reconcile every accepted request."""
    return build_dev_replay_request_set(
        scan_plan=scan_plan, evidence=_restored_scan_days(
            project_dir=project_dir, day_hashes=day_hashes, scan_plan=scan_plan,
            split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
        ), split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
    )


def require_source_bound_replay_request_set(
    request_set: DevReplayRequestSet, *, project_dir: Path, scan_plan: DevScanPlan,
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
) -> None:
    """Recompute all referenced days, including rejected and NO_SIGNAL slots, before reuse."""
    request_set = DevReplayRequestSet.model_validate(request_set.model_dump(mode="json"))
    observed = build_source_bound_replay_request_set(
        project_dir=project_dir, day_hashes=request_set.day_evidence_hashes, scan_plan=scan_plan,
        split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
    )
    if observed != request_set:
        raise CandleInputError("saved request set differs from the complete recomputed sources")


def verify_source_bound_request_set_market_data(
    *, project_dir: Path, request_set: DevReplayRequestSet,
    pairs: tuple[DevRequestMarketDataPair, ...], scan_plan: DevScanPlan,
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
) -> DevRequestSetMarketDataReport:
    """Recompute source days before verifying every saved 1m/Funding response pair."""
    return verify_dev_request_set_market_data(
        project_dir=project_dir, request_set=request_set, pairs=pairs, scan_plan=scan_plan,
        evidence=_restored_scan_days(
            project_dir=project_dir, day_hashes=request_set.day_evidence_hashes,
            scan_plan=scan_plan,
            split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
        ), split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
    )


def require_source_bound_market_data_report(
    report: DevRequestSetMarketDataReport, *, project_dir: Path, request_set: DevReplayRequestSet,
    scan_plan: DevScanPlan, split: ResearchSplitManifest, plan: SensitivityPlan,
    parameter: DevParameterVersion, snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> None:
    """Even a saved matching report requires fresh source and market-response validation."""
    require_request_set_market_data_report_binding(
        report, project_dir=project_dir, request_set=request_set, scan_plan=scan_plan,
        evidence=_restored_scan_days(
            project_dir=project_dir, day_hashes=request_set.day_evidence_hashes,
            scan_plan=scan_plan,
            split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
        ), split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
    )
