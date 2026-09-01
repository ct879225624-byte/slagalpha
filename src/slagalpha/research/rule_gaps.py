"""DEV-only evidence collection priorities, never a rule-coverage authorization."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.backtest.historical import (
    HistoricalReplayBlockReason,
    evaluate_universe_rule_gate,
)
from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.splits import (
    DatasetRole,
    ResearchSplitError,
    ResearchSplitManifest,
    audit_research_inputs,
)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class RuleGapWindow(BaseModel):
    """Consecutive selection dates with the same blocked 00:15 UTC gate reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: date
    end_exclusive: date
    reason_codes: tuple[HistoricalReplayBlockReason, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.start >= self.end_exclusive:
            raise ValueError("rule gap window must be non-empty")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("rule gap reasons must be unique and canonical")
        return self


class RuleGapTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    blocked_member_day_count: int = Field(gt=0)
    windows: tuple[RuleGapWindow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.blocked_member_day_count != sum(
            (window.end_exclusive - window.start).days for window in self.windows
        ):
            raise ValueError("rule gap window counts do not reconcile")
        for previous, current in zip(self.windows, self.windows[1:], strict=False):
            if previous.end_exclusive > current.start:
                raise ValueError("rule gap windows must be ordered and non-overlapping")
            if (previous.end_exclusive == current.start
                    and previous.reason_codes == current.reason_codes):
                raise ValueError("adjacent rule gap windows with identical reasons must merge")
        return self


class DevRuleGapReport(BaseModel):
    """Prioritized evidence requests, not inferred rule intervals or strategy results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-rule-gaps/0.1.0"] = "dev-rule-gaps/0.1.0"
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    coverage_scope: Literal["EFFECTIVE_FROM_GATE_ONLY"] = "EFFECTIVE_FROM_GATE_ONLY"
    split_hash: str
    input_audit_hash: str
    contract_registry_version: str
    registry_content_hash: str
    start: date
    end_exclusive: date
    member_day_count: int = Field(ge=0)
    eligible_member_day_count: int = Field(ge=0)
    blocked_member_day_count: int = Field(ge=0)
    targets: tuple[RuleGapTarget, ...]
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.start >= self.end_exclusive:
            raise ValueError("DEV interval must be non-empty")
        if self.member_day_count != self.eligible_member_day_count + self.blocked_member_day_count:
            raise ValueError("DEV member-day counts do not reconcile")
        if self.blocked_member_day_count != sum(t.blocked_member_day_count for t in self.targets):
            raise ValueError("DEV target counts do not reconcile")
        keys = tuple((-target.blocked_member_day_count, target.symbol) for target in self.targets)
        if keys != tuple(sorted(keys)) or len({t.symbol for t in self.targets}) != len(keys):
            raise ValueError("DEV targets must be unique and ordered by count then symbol")
        for target in self.targets:
            if any(w.start < self.start or w.end_exclusive > self.end_exclusive
                   for w in target.windows):
                raise ValueError("rule gap window is outside DEV")
        for value in (self.split_hash, self.input_audit_hash, self.registry_content_hash):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("rule gap evidence references must be SHA-256")
        if self.report_hash != _hash(self.model_dump(mode="json", exclude={"report_hash"})):
            raise ValueError("rule gap content hash mismatch")
        return self


def build_dev_rule_gap_report(
    *,
    split: ResearchSplitManifest,
    snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
) -> DevRuleGapReport:
    """Validate the full metadata sequence, then prioritize only DEV blocked gates."""

    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    registry = ContractRegistry.model_validate(registry.model_dump(mode="json"))
    audit = audit_research_inputs(split=split, snapshots=snapshots, registry=registry)
    dev = split.segments[0]
    dev_audit = audit.roles[0]
    windows: dict[str, list[RuleGapWindow]] = defaultdict(list)
    for snapshot in sorted(snapshots, key=lambda item: item.selected_at):
        day = snapshot.selected_at.date()
        if not dev.start <= day < dev.end_exclusive:
            continue
        for entry in evaluate_universe_rule_gate(snapshot, registry).entries:
            if entry.eligible:
                continue
            prior = windows[entry.symbol]
            start = day
            if (prior and prior[-1].end_exclusive == day
                    and prior[-1].reason_codes == entry.reason_codes):
                start = prior.pop().start
            prior.append(RuleGapWindow(
                start=start, end_exclusive=day + timedelta(days=1), reason_codes=entry.reason_codes
            ))
    targets = tuple(sorted(
        (RuleGapTarget(
            symbol=symbol,
            blocked_member_day_count=sum((w.end_exclusive - w.start).days for w in items),
            windows=tuple(items),
        ) for symbol, items in windows.items()),
        key=lambda target: (-target.blocked_member_day_count, target.symbol),
    ))
    payload = {
        "schema_version": "dev-rule-gaps/0.1.0",
        "dataset_role": DatasetRole.DEV.value,
        "coverage_scope": "EFFECTIVE_FROM_GATE_ONLY",
        "split_hash": split.split_hash,
        "input_audit_hash": audit.report_hash,
        "contract_registry_version": registry.registry_version,
        "registry_content_hash": _hash(registry.model_dump(mode="json")),
        "start": dev.start.isoformat(),
        "end_exclusive": dev.end_exclusive.isoformat(),
        "member_day_count": dev_audit.member_day_count,
        "eligible_member_day_count": dev_audit.rule_eligible_member_day_count,
        "blocked_member_day_count": dev_audit.rule_blocked_member_day_count,
        "targets": [target.model_dump(mode="json") for target in targets],
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    return DevRuleGapReport.model_validate({**payload, "report_hash": _hash(payload)})


def write_dev_rule_gap_report(report: DevRuleGapReport, data_dir: Path) -> Path:
    report = DevRuleGapReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests" / "dev_rule_gaps" / f"{report.report_hash}.json"
    content = (json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise ResearchSplitError(f"existing rule gap report changed: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
