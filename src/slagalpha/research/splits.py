"""Immutable global time splits and pre-research input audits for P9."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.backtest.historical import evaluate_universe_rule_gate
from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot


class ResearchSplitError(RuntimeError):
    """Raised when research roles, input coverage or immutable artifacts conflict."""


class DatasetRole(StrEnum):
    """Frozen global-time role for one research date."""

    DEV = "DEV"
    VALIDATION = "VALIDATION"
    LOCKED_TEST = "LOCKED_TEST"


class ResearchReadiness(StrEnum):
    """Whether a role can enter real Trade Plan/P7 research."""

    READY = "READY"
    BLOCKED = "BLOCKED"


class ResearchSplitSegment(BaseModel):
    """One half-open global UTC date interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DatasetRole
    start: date
    end_exclusive: date
    day_count: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_segment(self) -> Self:
        if self.end_exclusive <= self.start:
            raise ValueError("research split segment must be non-empty")
        if self.day_count != (self.end_exclusive - self.start).days:
            raise ValueError("research split day_count does not match dates")
        return self


class ResearchSplitManifest(BaseModel):
    """Frozen 50%/25%/25% global-time split."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["research-split/0.1.0"] = "research-split/0.1.0"
    research_start: date
    research_end_exclusive: date
    total_day_count: int = Field(gt=0)
    segments: tuple[ResearchSplitSegment, ...]
    universe_batch_run_version: str
    daily_snapshot_hash: str
    split_hash: str

    @field_validator(
        "universe_batch_run_version", "daily_snapshot_hash", "split_hash"
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("research split hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.total_day_count != (
            self.research_end_exclusive - self.research_start
        ).days:
            raise ValueError("research split total_day_count does not match dates")
        if tuple(segment.role for segment in self.segments) != tuple(DatasetRole):
            raise ValueError("research split roles must be DEV, VALIDATION, LOCKED_TEST")
        if self.segments[0].start != self.research_start:
            raise ValueError("research split does not start at research_start")
        for previous, current in zip(self.segments, self.segments[1:], strict=False):
            if previous.end_exclusive != current.start:
                raise ValueError("research split segments must be contiguous")
        if self.segments[-1].end_exclusive != self.research_end_exclusive:
            raise ValueError("research split does not end at research_end_exclusive")
        expected_counts = (
            self.total_day_count // 2,
            self.total_day_count // 4,
            self.total_day_count // 4,
        )
        if tuple(segment.day_count for segment in self.segments) != expected_counts:
            raise ValueError("research split is not exactly 50%/25%/25%")
        payload = self.model_dump(mode="json", exclude={"schema_version", "split_hash"})
        if self.split_hash != _canonical_sha256(payload):
            raise ValueError("research split content hash mismatch")
        return self


class ResearchRoleAudit(BaseModel):
    """Input readiness summary for one frozen dataset role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DatasetRole
    expected_day_count: int = Field(ge=0)
    snapshot_day_count: int = Field(ge=0)
    member_day_count: int = Field(ge=0)
    rule_eligible_member_day_count: int = Field(ge=0)
    rule_blocked_member_day_count: int = Field(ge=0)
    rule_reason_counts: dict[str, int]
    readiness: ResearchReadiness
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def validate_audit(self) -> Self:
        if self.member_day_count != (
            self.rule_eligible_member_day_count + self.rule_blocked_member_day_count
        ):
            raise ValueError("research member-day counts do not reconcile")
        if self.readiness is ResearchReadiness.READY and self.blockers:
            raise ValueError("READY role cannot contain blockers")
        if self.readiness is ResearchReadiness.BLOCKED and not self.blockers:
            raise ValueError("BLOCKED role requires blockers")
        return self


class ResearchInputAuditReport(BaseModel):
    """Fail-closed P9 input audit without executing any locked-test strategy run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["research-input-audit/0.1.0"] = (
        "research-input-audit/0.1.0"
    )
    split_hash: str
    contract_registry_version: str
    daily_snapshot_hash: str
    roles: tuple[ResearchRoleAudit, ...]
    overall_readiness: ResearchReadiness
    locked_test_consumed: Literal[False] = False
    deferred_checks: tuple[str, ...]
    report_hash: str

    @field_validator("split_hash", "daily_snapshot_hash", "report_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("research audit hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if tuple(role.role for role in self.roles) != tuple(DatasetRole):
            raise ValueError("research audit roles must use canonical order")
        expected = (
            ResearchReadiness.READY
            if all(role.readiness is ResearchReadiness.READY for role in self.roles)
            else ResearchReadiness.BLOCKED
        )
        if self.overall_readiness is not expected:
            raise ValueError("overall_readiness does not match roles")
        payload = self.model_dump(mode="json", exclude={"schema_version", "report_hash"})
        if self.report_hash != _canonical_sha256(payload):
            raise ValueError("research audit content hash mismatch")
        return self


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def snapshot_sequence_hash(snapshots: tuple[UniverseSnapshot, ...]) -> str:
    """Hash the canonical ordered date/version sequence used by Universe batches."""

    ordered = sorted(snapshots, key=lambda snapshot: snapshot.selected_at)
    payload = [
        {
            "selection_date": snapshot.selected_at.date().isoformat(),
            "universe_version": snapshot.universe_version,
        }
        for snapshot in ordered
    ]
    return _canonical_sha256(payload)


def build_research_split(
    *,
    research_start: date,
    research_end_exclusive: date,
    universe_batch_run_version: str,
    daily_snapshot_hash: str,
) -> ResearchSplitManifest:
    """Freeze exact global UTC boundaries; ambiguous remainder days fail closed."""

    total_days = (research_end_exclusive - research_start).days
    if total_days <= 0 or total_days % 4:
        raise ResearchSplitError("research interval must contain a positive multiple of 4 days")
    dev_days = total_days // 2
    quarter_days = total_days // 4
    dev_end = research_start.fromordinal(research_start.toordinal() + dev_days)
    validation_end = dev_end.fromordinal(dev_end.toordinal() + quarter_days)
    segments = (
        ResearchSplitSegment(
            role=DatasetRole.DEV,
            start=research_start,
            end_exclusive=dev_end,
            day_count=dev_days,
        ),
        ResearchSplitSegment(
            role=DatasetRole.VALIDATION,
            start=dev_end,
            end_exclusive=validation_end,
            day_count=quarter_days,
        ),
        ResearchSplitSegment(
            role=DatasetRole.LOCKED_TEST,
            start=validation_end,
            end_exclusive=research_end_exclusive,
            day_count=quarter_days,
        ),
    )
    payload = {
        "research_start": research_start.isoformat(),
        "research_end_exclusive": research_end_exclusive.isoformat(),
        "total_day_count": total_days,
        "segments": [segment.model_dump(mode="json") for segment in segments],
        "universe_batch_run_version": universe_batch_run_version,
        "daily_snapshot_hash": daily_snapshot_hash,
    }
    return ResearchSplitManifest(
        research_start=research_start,
        research_end_exclusive=research_end_exclusive,
        total_day_count=total_days,
        segments=segments,
        universe_batch_run_version=universe_batch_run_version,
        daily_snapshot_hash=daily_snapshot_hash,
        split_hash=_canonical_sha256(payload),
    )


def role_for_date(manifest: ResearchSplitManifest, value: date) -> DatasetRole:
    """Return the sole frozen role for a research date."""

    for segment in manifest.segments:
        if segment.start <= value < segment.end_exclusive:
            return segment.role
    raise ResearchSplitError(f"date {value.isoformat()} is outside the research interval")


def require_research_role(role: DatasetRole) -> None:
    """Allow DEV/VALIDATION only; locked execution needs a separate one-time gate."""

    if role is DatasetRole.LOCKED_TEST:
        raise ResearchSplitError("LOCKED_TEST access requires an explicit one-time gate")
    if role not in (DatasetRole.DEV, DatasetRole.VALIDATION):
        raise ResearchSplitError("unsupported research dataset role")


def audit_research_inputs(
    *,
    split: ResearchSplitManifest,
    snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> ResearchInputAuditReport:
    """Audit all member-days without running signals, P7, or the locked test."""

    ordered = tuple(sorted(snapshots, key=lambda snapshot: snapshot.selected_at))
    dates = tuple(snapshot.selected_at.date() for snapshot in ordered)
    expected_dates = tuple(
        split.research_start.fromordinal(split.research_start.toordinal() + offset)
        for offset in range(split.total_day_count)
    )
    if dates != expected_dates:
        raise ResearchSplitError("Universe snapshots do not exactly cover the split interval")
    sequence_hash = snapshot_sequence_hash(ordered)
    if sequence_hash != split.daily_snapshot_hash:
        raise ResearchSplitError("Universe snapshot sequence hash does not match split")

    counters: dict[DatasetRole, Counter[str]] = {
        role: Counter() for role in DatasetRole
    }
    reason_counters: dict[DatasetRole, Counter[str]] = {
        role: Counter() for role in DatasetRole
    }
    for snapshot in ordered:
        role = role_for_date(split, snapshot.selected_at.date())
        gate = evaluate_universe_rule_gate(snapshot, registry)
        counters[role]["snapshot_days"] += 1
        counters[role]["member_days"] += gate.member_count
        counters[role]["eligible_member_days"] += gate.eligible_count
        counters[role]["blocked_member_days"] += gate.blocked_count
        reason_counters[role].update(gate.reason_counts)

    audits: list[ResearchRoleAudit] = []
    for segment in split.segments:
        values = counters[segment.role]
        blockers: list[str] = []
        if values["snapshot_days"] != segment.day_count:
            blockers.append("UNIVERSE_DAY_COVERAGE_INCOMPLETE")
        if values["eligible_member_days"] == 0:
            blockers.append("NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS")
        audits.append(
            ResearchRoleAudit(
                role=segment.role,
                expected_day_count=segment.day_count,
                snapshot_day_count=values["snapshot_days"],
                member_day_count=values["member_days"],
                rule_eligible_member_day_count=values["eligible_member_days"],
                rule_blocked_member_day_count=values["blocked_member_days"],
                rule_reason_counts=dict(sorted(reason_counters[segment.role].items())),
                readiness=(
                    ResearchReadiness.BLOCKED
                    if blockers
                    else ResearchReadiness.READY
                ),
                blockers=tuple(blockers),
            )
        )
    overall = (
        ResearchReadiness.READY
        if all(audit.readiness is ResearchReadiness.READY for audit in audits)
        else ResearchReadiness.BLOCKED
    )
    deferred_checks = (
        "Generate P4-P6 signals only after historical rule member-days are available",
        "Download 1m/Funding/Aggregate Trades only for accepted replay requests",
        "Do not consume LOCKED_TEST before a frozen ParameterVersion exists",
    )
    payload = {
        "split_hash": split.split_hash,
        "contract_registry_version": registry.registry_version,
        "daily_snapshot_hash": sequence_hash,
        "roles": [audit.model_dump(mode="json") for audit in audits],
        "overall_readiness": overall.value,
        "locked_test_consumed": False,
        "deferred_checks": deferred_checks,
    }
    return ResearchInputAuditReport(
        split_hash=split.split_hash,
        contract_registry_version=registry.registry_version,
        daily_snapshot_hash=sequence_hash,
        roles=tuple(audits),
        overall_readiness=overall,
        locked_test_consumed=False,
        deferred_checks=deferred_checks,
        report_hash=_canonical_sha256(payload),
    )


def _write_model(model: BaseModel, destination: Path) -> Path:
    content = (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise ResearchSplitError(
                f"existing content-addressed research file changed: {destination}"
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_research_split(manifest: ResearchSplitManifest, data_dir: Path) -> Path:
    return _write_model(
        manifest,
        data_dir / "manifests" / "research_split" / f"{manifest.split_hash}.json",
    )


def write_research_input_audit(
    report: ResearchInputAuditReport,
    data_dir: Path,
) -> Path:
    return _write_model(
        report,
        data_dir
        / "manifests"
        / "research_input_audit"
        / f"{report.report_hash}.json",
    )
