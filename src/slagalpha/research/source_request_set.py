"""Rebuild complete DEV requests from recovered raw-source-validated days, not declarations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from pydantic import TypeAdapter

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.request_set import (
    DevReplayRequestSet,
    DevScanDayEvidence,
    build_dev_replay_request_set,
)
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.scan_storage import restore_source_bound_scan_day
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest


def _restored_scan_days(
    *, project_dir: Path, day_hashes: tuple[str, ...], scan_plan: DevScanPlan,
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
) -> Iterator[DevScanDayEvidence]:
    hashes = TypeAdapter(tuple[Sha256, ...]).validate_python(day_hashes)
    if len(hashes) != len(scan_plan.days) or len(set(hashes)) != len(hashes):
        raise CandleInputError("source day hashes must cover the complete DEV plan without repeats")
    for expected, digest in zip(scan_plan.days, hashes, strict=True):
        day = restore_source_bound_scan_day(
            project_dir=project_dir, content_hash=digest, scan_plan=scan_plan,
            split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
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
