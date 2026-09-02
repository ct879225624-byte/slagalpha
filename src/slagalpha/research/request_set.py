"""Exact scan-record/request reconciliation, not proof that strategy evidence is genuine."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.domain.symbols import normalize_symbol
from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    ReplayDataInputError,
    Sha256,
    build_dev_replay_data_request,
)
from slagalpha.research.scan_plan import (
    DevScanDayPlan,
    DevScanPlan,
    build_dev_scan_plan,
    model_hash,
)
from slagalpha.research.sensitivity import SensitivityPlan, require_dev_execution_inputs
from slagalpha.research.splits import DatasetRole, ResearchSplitManifest, audit_research_inputs
from slagalpha.strategy.plans import TakeProfitEvaluation


class DevScanRecord(BaseModel):
    """A claimed per-slot result; its source reference is not verified by this module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    confirmation_close: datetime
    outcome: Literal["NO_SIGNAL", "REJECTED_PLAN", "ACCEPTED_PLAN", "BLOCKED"]
    reason_codes: tuple[str, ...] = Field(min_length=1)
    upstream_evidence_hash: Sha256
    trade_plan: TakeProfitEvaluation | None = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if value != normalize_symbol(value):
            raise ValueError("scan symbol must be canonical")
        return value

    @field_validator("confirmation_close")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if (value.tzinfo is None or value.utcoffset() != timedelta(0)
            or value.minute % 15 or value.second or value.microsecond):
            raise ValueError("scan confirmation must be a closed UTC 15m boundary")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (self.reason_codes != tuple(sorted(set(self.reason_codes)))
            or any(not code.strip() or code != code.strip() for code in self.reason_codes)):
            raise ValueError("scan reasons must be nonempty, unique and canonical")
        if self.outcome == "ACCEPTED_PLAN":
            if self.trade_plan is None or not self.trade_plan.accepted:
                raise ValueError("accepted scan result requires an accepted Trade Plan")
            request = self.trade_plan.entry_stop.request
            if (request.symbol != self.symbol
                or request.confirmation_close != self.confirmation_close):
                raise ValueError("Trade Plan does not match scan slot")
        elif self.trade_plan is not None:
            raise ValueError("only an accepted scan result may carry a Trade Plan")
        return self


class DevScanDayEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-scan-day-evidence/0.1.0"] = "dev-scan-day-evidence/0.1.0"
    scan_plan_hash: Sha256
    day_plan: DevScanDayPlan
    records: tuple[DevScanRecord, ...]
    strategy_evidence_verified: Literal[False] = False
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        expected = tuple(
            (self.day_plan.first_confirmation + timedelta(minutes=15 * index), symbol)
            for index in range(self.day_plan.time_count) for symbol in self.day_plan.symbols
        )
        observed = tuple((item.confirmation_close, item.symbol) for item in self.records)
        if observed != expected:
            raise ValueError("scan records must exactly cover each canonical time/symbol slot")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("scan evidence content hash mismatch")
        return self


def build_dev_scan_day_evidence(
    *, scan_plan: DevScanPlan, selection_date: date, records: tuple[DevScanRecord, ...],
) -> DevScanDayEvidence:
    scan_plan = DevScanPlan.model_validate(scan_plan.model_dump(mode="json"))
    day = next((item for item in scan_plan.days if item.selection_date == selection_date), None)
    if day is None:
        raise ReplayDataInputError("scan day does not belong to the DEV scan plan")
    records = tuple(DevScanRecord.model_validate(item.model_dump(mode="json")) for item in records)
    payload = {
        "schema_version": "dev-scan-day-evidence/0.1.0", "scan_plan_hash": scan_plan.plan_hash,
        "day_plan": day.model_dump(mode="json"),
        "records": [item.model_dump(mode="json") for item in records],
        "strategy_evidence_verified": False,
    }
    return DevScanDayEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


class DevReplayRequestSet(BaseModel):
    """All accepted requests for the declared scan records; never a global readiness gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-replay-request-set/0.1.0"] = "dev-replay-request-set/0.1.0"
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    scan_plan_hash: Sha256
    registry_content_hash: Sha256
    day_evidence_hashes: tuple[Sha256, ...] = Field(min_length=1)
    scan_record_count: int = Field(gt=0, strict=True)
    requests: tuple[DevReplayDataRequest, ...] = Field(min_length=1)
    strategy_evidence_verified: Literal[False] = False
    download_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_requests(self) -> Self:
        keys = tuple((item.start, item.request.armed.symbol) for item in self.requests)
        signal_ids = tuple(item.request.armed.logical_signal_id for item in self.requests)
        if keys != tuple(sorted(set(keys))) or len(set(signal_ids)) != len(signal_ids):
            raise ValueError("request set must have unique canonical slots and signal ids")
        if len(set(self.day_evidence_hashes)) != len(self.day_evidence_hashes):
            raise ValueError("request set cannot repeat daily evidence")
        if self.scan_record_count < len(self.requests):
            raise ValueError("accepted requests cannot exceed scan records")
        first = self.requests[0]
        context = (first.split_hash, first.sensitivity_plan_hash,
                   first.parameter_content_hash, self.registry_content_hash)
        if any((item.split_hash, item.sensitivity_plan_hash, item.parameter_content_hash,
                item.registry_content_hash) != context for item in self.requests):
            raise ValueError("request set cannot mix research contexts")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("request set content hash mismatch")
        return self


def build_dev_replay_request_set(
    *, scan_plan: DevScanPlan, evidence: Iterable[DevScanDayEvidence],
    split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...], registry: ContractRegistry,
) -> DevReplayRequestSet:
    """Rebuild scope/audit before consuming daily evidence; never silently drop accepted plans."""
    scan_plan = DevScanPlan.model_validate(scan_plan.model_dump(mode="json"))
    current = build_dev_scan_plan(split=split, plan=plan, parameter=parameter, snapshots=snapshots)
    if current != scan_plan:
        raise ReplayDataInputError("scan plan does not match current source evidence")
    registry = ContractRegistry.model_validate(registry.model_dump(mode="json"))
    audit = audit_research_inputs(split=split, snapshots=snapshots, registry=registry)
    require_dev_execution_inputs(plan, audit)
    by_date = {item.selected_at.date(): item for item in snapshots}
    source = iter(evidence)
    day_hashes: list[str] = []
    requests: list[DevReplayDataRequest] = []
    for day in scan_plan.days:
        saved = next(source, None)
        if saved is None:
            raise ReplayDataInputError("missing DEV scan day evidence")
        saved = DevScanDayEvidence.model_validate(saved.model_dump(mode="json"))
        if saved.scan_plan_hash != scan_plan.plan_hash or saved.day_plan != day:
            raise ReplayDataInputError("scan day evidence does not match its exact plan position")
        if any(record.outcome == "BLOCKED" for record in saved.records):
            raise ReplayDataInputError("blocked scan slots cannot become a complete request set")
        day_hashes.append(saved.content_hash)
        for record in saved.records:
            if record.trade_plan is not None:
                requests.append(build_dev_replay_data_request(
                    trade_plan=record.trade_plan, plan=plan, parameter=parameter, split=split,
                    universe=by_date[day.selection_date], registry=registry,
                ))
    if next(source, None) is not None:
        raise ReplayDataInputError("extra DEV scan day evidence")
    if not requests:
        raise ReplayDataInputError("empty request sets require separate no-signal research review")
    payload: dict[str, Any] = {
        "schema_version": "dev-replay-request-set/0.1.0", "dataset_role": "DEV",
        "scan_plan_hash": scan_plan.plan_hash, "registry_content_hash": model_hash(registry),
        "day_evidence_hashes": day_hashes, "scan_record_count": scan_plan.expected_record_count,
        "requests": [item.model_dump(mode="json") for item in requests],
        "strategy_evidence_verified": False, "download_authorized": False,
        "research_authorized": False,
    }
    return DevReplayRequestSet.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def require_replay_request_set_binding(
    request_set: DevReplayRequestSet, *, scan_plan: DevScanPlan,
    evidence: Iterable[DevScanDayEvidence], split: ResearchSplitManifest, plan: SensitivityPlan,
    parameter: DevParameterVersion, snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> None:
    observed = build_dev_replay_request_set(
        scan_plan=scan_plan, evidence=evidence, split=split, plan=plan, parameter=parameter,
        snapshots=snapshots, registry=registry,
    )
    if observed != request_set:
        raise ReplayDataInputError("saved request set does not match complete scan evidence")


def write_dev_request_evidence(
    artifact: DevScanDayEvidence | DevReplayRequestSet, data_dir: Path,
) -> Path:
    """Persist claimed records or reconciled requests, preserving the unverified evidence flag."""
    artifact = type(artifact).model_validate(artifact.model_dump(mode="json"))
    folder = ("dev_scan_day_evidence" if isinstance(artifact, DevScanDayEvidence)
              else "replay_request_set")
    destination = data_dir / "manifests" / folder / f"{artifact.content_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ReplayDataInputError("request evidence path escapes data directory")
    content = canonical_json_bytes(artifact.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ReplayDataInputError("existing request evidence changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
