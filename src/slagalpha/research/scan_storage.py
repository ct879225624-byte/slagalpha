"""Immutable daily source checkpoints; loading declarations never replaces raw revalidation."""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.reporting.run_manifest import _load_object, _publish_immutable, canonical_json_bytes
from slagalpha.research.candle_history import ScanHistoryLineage
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.request_set import DevScanDayEvidence
from slagalpha.research.scan_days import require_source_bound_scan_day
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest


def _artifact_path(project_dir: Path, folder: str, content_hash: str) -> Path:
    digest = TypeAdapter(Sha256).validate_python(content_hash)
    path = project_dir
    for part in ("", "data", "manifests", folder, f"{digest}.json"):
        if part:
            path /= part
        if path.is_symlink() or path.is_junction():
            raise CandleInputError("scan checkpoint path cannot contain a symlink or junction")
    if not path.resolve().is_relative_to(project_dir.resolve()):
        raise CandleInputError("scan checkpoint path escapes project")
    return path


def read_declared_scan_source(*, project_dir: Path, content_hash: str) -> ScanTradePlanEvidence:
    """Check saved JSON/schema/hash only. Consumers must still revalidate the source files."""
    path = _artifact_path(project_dir, "scan_trade_plan_source", content_hash)
    source = ScanTradePlanEvidence.model_validate(_load_object(path.read_bytes()))
    if source.content_hash != content_hash:
        raise CandleInputError("saved scan source does not match its filename identity")
    return source


def save_source_bound_scan_day(
    evidence: DevScanDayEvidence, *, project_dir: Path, sources: tuple[ScanTradePlanEvidence, ...],
    scan_plan: DevScanPlan, split: ResearchSplitManifest, plan: SensitivityPlan,
    parameter: DevParameterVersion, snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> Path:
    """Revalidate first, publish sources first and day last; never repair a completed day."""
    evidence = DevScanDayEvidence.model_validate(evidence.model_dump(mode="json"))
    sources = tuple(ScanTradePlanEvidence.model_validate(item.model_dump(mode="json"))
                    for item in sources)
    if tuple(item.content_hash for item in sources) != tuple(
        record.upstream_evidence_hash for record in evidence.records
    ):
        raise CandleInputError("saved daily sources must match every ordered record reference")
    require_source_bound_scan_day(
        evidence, project_dir=project_dir, scan_plan=scan_plan, sources=sources,
        split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
    )
    day_path = _artifact_path(project_dir, "source_scan_day", evidence.content_hash)
    day_content = canonical_json_bytes(evidence.model_dump(mode="json"))
    source_contents = {
        _artifact_path(project_dir, "scan_trade_plan_source", item.content_hash):
        canonical_json_bytes(item.model_dump(mode="json")) for item in sources
    }
    if day_path.exists() and any(not path.is_file() for path in source_contents):
        raise CandleInputError("completed source day is missing sources; refusing silent repair")
    for path, content in (*source_contents.items(), (day_path, day_content)):
        if path.exists() and (not path.is_file() or path.read_bytes() != content):
            raise CandleInputError("existing scan checkpoint changed")
    for path, content in (*source_contents.items(), (day_path, day_content)):
        path.parent.mkdir(parents=True, exist_ok=True)
        _publish_immutable(path, content)
    return day_path


def restore_source_bound_scan_day(
    *, project_dir: Path, content_hash: str, scan_plan: DevScanPlan,
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
    history_lineage: ScanHistoryLineage | None = None,
) -> DevScanDayEvidence:
    """Recover only a completed day, then reread every source and recompute every slot."""
    path = _artifact_path(project_dir, "source_scan_day", content_hash)
    evidence = DevScanDayEvidence.model_validate(_load_object(path.read_bytes()))
    if evidence.content_hash != content_hash:
        raise CandleInputError("saved source day does not match its filename identity")
    sources = (read_declared_scan_source(
        project_dir=project_dir, content_hash=record.upstream_evidence_hash,
    ) for record in evidence.records)
    require_source_bound_scan_day(
        evidence, project_dir=project_dir, scan_plan=scan_plan, sources=sources,
        split=split, plan=plan, parameter=parameter, snapshots=snapshots, registry=registry,
        history_lineage=history_lineage,
    )
    return evidence
