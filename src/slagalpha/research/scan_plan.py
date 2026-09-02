"""Exact DEV scan obligations derived from frozen daily Universe metadata, not signals."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.domain.universe import UniverseSnapshot
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.parameters import DevParameterVersion, require_parameter_plan_binding
from slagalpha.research.replay_inputs import ReplayDataInputError, Sha256
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import DatasetRole, ResearchSplitManifest, snapshot_sequence_hash


def model_hash(model: BaseModel) -> str:
    return hashlib.sha256(canonical_json_bytes(model.model_dump(mode="json"))).hexdigest()


class DevScanDayPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selection_date: date
    universe_version: Sha256
    universe_content_hash: Sha256
    symbols: tuple[str, ...]
    first_confirmation: datetime
    end_exclusive: datetime
    time_count: int = Field(gt=0, le=96, strict=True)
    record_count: int = Field(ge=0, strict=True)

    @field_validator("first_confirmation", "end_exclusive")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("scan times must be UTC")
        return value

    @model_validator(mode="after")
    def validate_day(self) -> Self:
        if self.symbols != tuple(sorted(set(self.symbols))) or len(self.symbols) > 30:
            raise ValueError("scan symbols must be unique and canonical")
        midnight = datetime.combine(self.selection_date, time(), UTC)
        if self.first_confirmation != midnight + timedelta(minutes=15):
            raise ValueError("scan day begins at Universe cutover 00:15 UTC")
        if self.end_exclusive not in (midnight + timedelta(days=1),
                                      midnight + timedelta(days=1, minutes=15)):
            raise ValueError("scan day ends at next cutover or the DEV boundary")
        seconds = (self.end_exclusive - self.first_confirmation).total_seconds()
        if self.time_count != int(seconds / 900):
            raise ValueError("scan time count does not match its interval")
        if self.record_count != self.time_count * len(self.symbols):
            raise ValueError("scan record count does not reconcile")
        return self


class DevScanPlan(BaseModel):
    """Obligations only: generating this manifest never counts as performing a scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-scan-plan/0.1.0"] = "dev-scan-plan/0.1.0"
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    split_hash: Sha256
    sensitivity_plan_hash: Sha256
    parameter_content_hash: Sha256
    daily_snapshot_hash: Sha256
    source_snapshot_content_hash: Sha256
    days: tuple[DevScanDayPlan, ...] = Field(min_length=1)
    expected_record_count: int = Field(ge=0, strict=True)
    upstream_blockers: tuple[str, ...]
    scan_executed: Literal[False] = False
    research_authorized: Literal[False] = False
    plan_hash: Sha256

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        for previous, current in zip(self.days, self.days[1:], strict=False):
            if (current.selection_date != previous.selection_date + timedelta(days=1)
                or previous.end_exclusive != current.first_confirmation):
                raise ValueError("scan days must be contiguous and canonical")
        if self.days[-1].end_exclusive.time() != time():
            raise ValueError("final scan day must stop at DEV midnight")
        if self.expected_record_count != sum(day.record_count for day in self.days):
            raise ValueError("total scan record count does not reconcile")
        if self.upstream_blockers != tuple(sorted(set(self.upstream_blockers))):
            raise ValueError("scan upstream blockers must be unique and canonical")
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("scan plan content hash mismatch")
        return self


def build_dev_scan_plan(
    *, split: ResearchSplitManifest, plan: SensitivityPlan, parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...],
) -> DevScanPlan:
    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    require_parameter_plan_binding(parameter, plan)
    if split.split_hash != plan.split_hash:
        raise ReplayDataInputError("scan split does not belong to the sensitivity plan")
    ordered = tuple(sorted((UniverseSnapshot.model_validate(item.model_dump(mode="json"))
                            for item in snapshots), key=lambda item: item.selected_at))
    expected_dates = tuple(split.research_start + timedelta(days=offset)
                           for offset in range(split.total_day_count))
    if tuple(item.selected_at.date() for item in ordered) != expected_dates:
        raise ReplayDataInputError("snapshot dates must exactly cover the frozen split")
    if snapshot_sequence_hash(ordered) != split.daily_snapshot_hash:
        raise ReplayDataInputError("snapshot sequence does not match the frozen split")
    dev = split.segments[0]
    end = datetime.combine(dev.end_exclusive, time(), UTC)
    days = []
    for snapshot in ordered:
        if not dev.start <= snapshot.selected_at.date() < dev.end_exclusive:
            continue
        day_end = min(snapshot.effective_to, end)
        count = int((day_end - snapshot.effective_from).total_seconds() / 900)
        days.append(DevScanDayPlan(
            selection_date=snapshot.selected_at.date(), universe_version=snapshot.universe_version,
            universe_content_hash=model_hash(snapshot),
            symbols=tuple(sorted(member.symbol for member in snapshot.members)),
            first_confirmation=snapshot.effective_from, end_exclusive=day_end,
            time_count=count, record_count=count * snapshot.member_count,
        ))
    source_content = {"snapshot_content_hashes": [model_hash(item) for item in ordered]}
    payload: dict[str, Any] = {
        "schema_version": "dev-scan-plan/0.1.0", "dataset_role": "DEV",
        "split_hash": split.split_hash, "sensitivity_plan_hash": plan.plan_hash,
        "parameter_content_hash": parameter.content_hash,
        "daily_snapshot_hash": split.daily_snapshot_hash,
        "source_snapshot_content_hash": hashlib.sha256(
            canonical_json_bytes(source_content)
        ).hexdigest(),
        "days": [day.model_dump(mode="json") for day in days],
        "expected_record_count": sum(day.record_count for day in days),
        "upstream_blockers": sorted(set(plan.blockers)),
        "scan_executed": False, "research_authorized": False,
    }
    return DevScanPlan.model_validate({
        **payload, "plan_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def write_dev_scan_plan(plan: DevScanPlan, data_dir: Path) -> Path:
    plan = DevScanPlan.model_validate(plan.model_dump(mode="json"))
    destination = data_dir / "manifests" / "dev_scan_plan" / f"{plan.plan_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ReplayDataInputError("scan plan path escapes data directory")
    content = canonical_json_bytes(plan.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ReplayDataInputError("existing scan plan changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
