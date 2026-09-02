"""Revalidate saved market data for every declared request without executing research."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_inputs import ReplayDataInputError, Sha256
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    load_replay_market_data_artifact,
)
from slagalpha.research.request_set import (
    DevReplayRequestSet,
    DevScanDayEvidence,
    require_replay_request_set_binding,
)
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest


class DevRequestMarketDataPair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_hash: Sha256
    one_minute: ReplayMarketDataArtifact
    funding: ReplayMarketDataArtifact

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.one_minute.role != "CANDLE_ONE_MINUTE" or self.funding.role != "FUNDING":
            raise ValueError("each request requires one 1m and one Funding artifact")
        if any(item.request_hash != self.request_hash for item in (self.one_minute, self.funding)):
            raise ValueError("both market-data artifacts must belong to the same request")
        return self


class DevRequestSetMarketDataReport(BaseModel):
    """A past byte-validation receipt, not a substitute for current source revalidation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-request-set-market-data/0.1.0"] = (
        "dev-request-set-market-data/0.1.0"
    )
    validation_scope: Literal["DECLARED_REQUEST_SET_SAVED_RESPONSES"] = (
        "DECLARED_REQUEST_SET_SAVED_RESPONSES"
    )
    request_set_hash: Sha256
    pairs: tuple[DevRequestMarketDataPair, ...] = Field(min_length=1)
    verified_request_count: int = Field(gt=0, strict=True)
    one_minute_record_count: int = Field(gt=0, strict=True)
    funding_record_count: int = Field(gt=0, strict=True)
    strategy_evidence_verified: Literal[False] = False
    replay_executed: Literal[False] = False
    research_authorized: Literal[False] = False
    report_hash: Sha256

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if len({pair.request_hash for pair in self.pairs}) != len(self.pairs):
            raise ValueError("market-data report cannot repeat a request")
        if self.verified_request_count != len(self.pairs):
            raise ValueError("verified request count does not reconcile")
        if self.one_minute_record_count != sum(pair.one_minute.record_count for pair in self.pairs):
            raise ValueError("1m record count does not reconcile")
        if self.funding_record_count != sum(pair.funding.record_count for pair in self.pairs):
            raise ValueError("Funding record count does not reconcile")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("market-data report content hash mismatch")
        return self


def verify_dev_request_set_market_data(
    *, project_dir: Path, request_set: DevReplayRequestSet,
    pairs: tuple[DevRequestMarketDataPair, ...], scan_plan: DevScanPlan,
    evidence: Iterable[DevScanDayEvidence], split: ResearchSplitManifest, plan: SensitivityPlan,
    parameter: DevParameterVersion, snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> DevRequestSetMarketDataReport:
    """Reject scope/coverage errors before IO; validate all bytes before returning any receipt."""
    request_set = DevReplayRequestSet.model_validate(request_set.model_dump(mode="json"))
    require_replay_request_set_binding(
        request_set, scan_plan=scan_plan, evidence=evidence, split=split, plan=plan,
        parameter=parameter, snapshots=snapshots, registry=registry,
    )
    pairs = tuple(DevRequestMarketDataPair.model_validate(pair.model_dump(mode="json"))
                  for pair in pairs)
    if tuple(pair.request_hash for pair in pairs) != tuple(
        request.request_hash for request in request_set.requests
    ):
        raise ReplayDataInputError("market-data pairs must exactly cover the ordered request set")
    for request, pair in zip(request_set.requests, pairs, strict=True):
        # These re-read raw bytes and recompute normalized content, one request at a time.
        # The complete original Trade Plan/Universe/rule binding was rechecked above.
        load_replay_market_data_artifact(
            project_dir=project_dir, request=request, artifact=pair.one_minute,
        )
        load_replay_market_data_artifact(
            project_dir=project_dir, request=request, artifact=pair.funding,
        )
    payload = {
        "schema_version": "dev-request-set-market-data/0.1.0",
        "validation_scope": "DECLARED_REQUEST_SET_SAVED_RESPONSES",
        "request_set_hash": request_set.content_hash,
        "pairs": [pair.model_dump(mode="json") for pair in pairs],
        "verified_request_count": len(pairs),
        "one_minute_record_count": sum(pair.one_minute.record_count for pair in pairs),
        "funding_record_count": sum(pair.funding.record_count for pair in pairs),
        "strategy_evidence_verified": False, "replay_executed": False,
        "research_authorized": False,
    }
    return DevRequestSetMarketDataReport.model_validate({
        **payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def require_request_set_market_data_report_binding(
    report: DevRequestSetMarketDataReport, *, project_dir: Path, request_set: DevReplayRequestSet,
    scan_plan: DevScanPlan, evidence: Iterable[DevScanDayEvidence], split: ResearchSplitManifest,
    plan: SensitivityPlan, parameter: DevParameterVersion, snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> None:
    report = DevRequestSetMarketDataReport.model_validate(report.model_dump(mode="json"))
    if report.request_set_hash != request_set.content_hash:
        raise ReplayDataInputError("market-data report belongs to a different request set")
    observed = verify_dev_request_set_market_data(
        project_dir=project_dir, request_set=request_set, pairs=report.pairs,
        scan_plan=scan_plan, evidence=evidence, split=split, plan=plan,
        parameter=parameter, snapshots=snapshots, registry=registry,
    )
    if observed != report:
        raise ReplayDataInputError("market-data report does not match current source evidence")


def write_request_set_market_data_report(
    report: DevRequestSetMarketDataReport, data_dir: Path,
) -> Path:
    report = DevRequestSetMarketDataReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests" / "request_set_market_data" / f"{report.report_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ReplayDataInputError("market-data report path escapes data directory")
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ReplayDataInputError("existing market-data report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
