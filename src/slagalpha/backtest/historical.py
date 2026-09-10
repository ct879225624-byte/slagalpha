"""Historical Universe and contract-rule gates in front of the P7 scheduler."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.backtest.replay import ReplayInputError
from slagalpha.backtest.runner import (
    ReplayCase,
    ReplayCaseResult,
    ReplayCaseStatus,
    replay_cases,
)
from slagalpha.domain.universe import (
    APPROXIMATE_TICK_SIZE_WARNING,
    ContractRegistry,
    ContractRegistryEntry,
    EvidenceConfidence,
    RegistryVerification,
    UniverseSnapshot,
)


class HistoricalReplayError(RuntimeError):
    """Raised when immutable historical replay evidence conflicts."""


class HistoricalReplayBlockReason(StrEnum):
    """Stable reasons that stop a case before P7 market-data replay."""

    SNAPSHOT_INACTIVE = "SNAPSHOT_INACTIVE"
    NOT_UNIVERSE_MEMBER = "NOT_UNIVERSE_MEMBER"
    CONTRACT_RULE_MISSING = "CONTRACT_RULE_MISSING"
    CONTRACT_RULE_UNVERIFIED = "CONTRACT_RULE_UNVERIFIED"
    TICK_SIZE_MISMATCH = "TICK_SIZE_MISMATCH"


class HistoricalReplayStatus(StrEnum):
    """Outcome of the historical context gate plus the existing P7 scheduler."""

    BLOCKED = "BLOCKED"
    EXECUTED = "EXECUTED"
    SKIPPED_ACTIVE_TRADE = "SKIPPED_ACTIVE_TRADE"


class HistoricalRuleGateEntry(BaseModel):
    """Contract-rule eligibility for one Universe member at one instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    eligible: bool
    reason_codes: tuple[HistoricalReplayBlockReason, ...]
    rule_source_ref: str | None
    rule_verification_status: RegistryVerification | None = None
    rule_confidence: EvidenceConfidence | None = None
    warning_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        canonical = tuple(sorted(set(self.reason_codes), key=lambda item: item.value))
        if self.reason_codes != canonical:
            raise ValueError("historical rule reasons must be unique and canonical")
        if self.eligible == bool(self.reason_codes):
            raise ValueError("eligible rule gate must have no reasons and vice versa")
        if self.eligible and self.rule_source_ref is None:
            raise ValueError("eligible rule gate requires a source reference")
        if self.warning_codes != tuple(sorted(set(self.warning_codes))):
            raise ValueError("historical rule warnings must be unique and canonical")
        if self.warning_codes and (
            not self.eligible
            or self.rule_verification_status is not RegistryVerification.UNVERIFIED
        ):
            raise ValueError("historical rule warnings require an eligible unverified rule")
        return self


class HistoricalRuleGateReport(BaseModel):
    """Immutable real-data evidence for the P7 contract-rule boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["historical-rule-gate/0.1.0"] = (
        "historical-rule-gate/0.1.0"
    )
    universe_version: str
    contract_registry_version: str
    evaluated_at: datetime
    member_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    entries: tuple[HistoricalRuleGateEntry, ...]
    reason_counts: dict[str, int]
    report_hash: str

    @field_validator("universe_version", "report_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("historical gate hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.member_count != len(self.entries):
            raise ValueError("member_count does not match entries")
        if self.eligible_count + self.blocked_count != self.member_count:
            raise ValueError("historical rule gate counts do not reconcile")
        if self.eligible_count != sum(entry.eligible for entry in self.entries):
            raise ValueError("eligible_count does not match entries")
        symbols = tuple(entry.symbol for entry in self.entries)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("historical rule gate entries must be unique and canonical")
        return self


class HistoricalReplayCaseResult(BaseModel):
    """One case either blocked by historical evidence or handled by P7."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_signal_id: str
    symbol: str
    confirmation_close: datetime
    status: HistoricalReplayStatus
    reason_codes: tuple[HistoricalReplayBlockReason, ...]
    rule_source_ref: str | None
    replay: ReplayCaseResult | None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        canonical = tuple(sorted(set(self.reason_codes), key=lambda item: item.value))
        if self.reason_codes != canonical:
            raise ValueError("historical replay reasons must be unique and canonical")
        if self.status is HistoricalReplayStatus.BLOCKED:
            if not self.reason_codes or self.replay is not None:
                raise ValueError("blocked historical replay requires reasons and no replay")
        elif self.reason_codes or self.replay is None:
            raise ValueError("P7-handled historical replay cannot have gate reasons")
        return self


class HistoricalReplayResult(BaseModel):
    """Canonical integration output for one daily Universe snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["historical-p7-replay/0.1.0"] = (
        "historical-p7-replay/0.1.0"
    )
    universe_version: str
    contract_registry_version: str
    cases: tuple[HistoricalReplayCaseResult, ...]
    result_hash: str

    @field_validator("universe_version", "result_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("historical replay hashes must be SHA-256")
        return normalized


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _active_rule(
    registry: ContractRegistry,
    symbol: str,
    at: datetime,
) -> ContractRegistryEntry | None:
    matches = tuple(
        entry
        for entry in registry.entries
        if entry.symbol == symbol
        and entry.effective_from <= at
        and (entry.effective_to is None or at < entry.effective_to)
    )
    if len(matches) > 1:
        raise HistoricalReplayError(f"multiple contract rules active for {symbol}")
    return matches[0] if matches else None


def _gate_reasons(
    *,
    universe: UniverseSnapshot,
    registry: ContractRegistry,
    symbol: str,
    at: datetime,
    tick_size: object | None = None,
    allow_approximate_rules: bool = False,
) -> tuple[tuple[HistoricalReplayBlockReason, ...], ContractRegistryEntry | None]:
    reasons: list[HistoricalReplayBlockReason] = []
    if not (universe.effective_from <= at < universe.effective_to):
        reasons.append(HistoricalReplayBlockReason.SNAPSHOT_INACTIVE)
    if symbol not in {member.symbol for member in universe.members}:
        reasons.append(HistoricalReplayBlockReason.NOT_UNIVERSE_MEMBER)
    rule = _active_rule(registry, symbol, at)
    if rule is None:
        reasons.append(HistoricalReplayBlockReason.CONTRACT_RULE_MISSING)
    elif not (
        rule.eligible_for_locked_research
        or (allow_approximate_rules and rule.eligible_for_dev_research)
    ):
        reasons.append(HistoricalReplayBlockReason.CONTRACT_RULE_UNVERIFIED)
    elif tick_size is not None and tick_size != rule.tick_size:
        reasons.append(HistoricalReplayBlockReason.TICK_SIZE_MISMATCH)
    return tuple(sorted(set(reasons), key=lambda item: item.value)), rule


def evaluate_universe_rule_gate(
    universe: UniverseSnapshot,
    registry: ContractRegistry,
    *,
    allow_approximate_rules: bool = False,
) -> HistoricalRuleGateReport:
    """Evaluate every Universe member before any P7 request or 1m data is needed."""

    entries: list[HistoricalRuleGateEntry] = []
    for member in sorted(universe.members, key=lambda item: item.symbol):
        reasons, rule = _gate_reasons(
            universe=universe,
            registry=registry,
            symbol=member.symbol,
            at=universe.effective_from,
            allow_approximate_rules=allow_approximate_rules,
        )
        warnings = (
            (APPROXIMATE_TICK_SIZE_WARNING,)
            if rule is not None
            and not reasons
            and rule.verification_status is RegistryVerification.UNVERIFIED
            else ()
        )
        entries.append(
            HistoricalRuleGateEntry(
                symbol=member.symbol,
                eligible=not reasons,
                reason_codes=reasons,
                rule_source_ref=None if rule is None else rule.source_ref,
                rule_verification_status=(
                    None if rule is None else rule.verification_status
                ),
                rule_confidence=None if rule is None else rule.confidence,
                warning_codes=warnings,
            )
        )
    reason_counts = dict(
        sorted(Counter(reason.value for entry in entries for reason in entry.reason_codes).items())
    )
    payload = {
        "universe_version": universe.universe_version,
        "contract_registry_version": registry.registry_version,
        "evaluated_at": universe.effective_from.isoformat(),
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "reason_counts": reason_counts,
    }
    report_hash = _canonical_sha256(payload)
    return HistoricalRuleGateReport(
        universe_version=universe.universe_version,
        contract_registry_version=registry.registry_version,
        evaluated_at=universe.effective_from,
        member_count=len(entries),
        eligible_count=sum(entry.eligible for entry in entries),
        blocked_count=sum(not entry.eligible for entry in entries),
        entries=tuple(entries),
        reason_counts=reason_counts,
        report_hash=report_hash,
    )


def replay_historical_cases(
    *,
    universe: UniverseSnapshot,
    registry: ContractRegistry,
    cases: tuple[ReplayCase, ...],
) -> HistoricalReplayResult:
    """Gate cases by frozen Universe/rules, then invoke the unchanged P7 scheduler."""

    signal_ids = tuple(case.request.armed.logical_signal_id for case in cases)
    if len(set(signal_ids)) != len(signal_ids):
        raise ReplayInputError("historical replay logical_signal_id values must be unique")
    ordered = tuple(
        sorted(
            cases,
            key=lambda case: (
                case.request.armed.confirmation_close,
                case.request.armed.symbol,
                case.request.armed.logical_signal_id,
            ),
        )
    )
    eligible: list[ReplayCase] = []
    blocked: dict[str, tuple[tuple[HistoricalReplayBlockReason, ...], str | None]] = {}
    for case in ordered:
        request = case.request
        reasons, rule = _gate_reasons(
            universe=universe,
            registry=registry,
            symbol=request.armed.symbol,
            at=request.armed.confirmation_close,
            tick_size=request.tick_size,
        )
        if reasons:
            blocked[request.armed.logical_signal_id] = (
                reasons,
                None if rule is None else rule.source_ref,
            )
        else:
            eligible.append(case)

    replayed = {
        result.logical_signal_id: result for result in replay_cases(tuple(eligible)).cases
    }
    results: list[HistoricalReplayCaseResult] = []
    for case in ordered:
        request = case.request
        logical_id = request.armed.logical_signal_id
        if logical_id in blocked:
            reasons, source_ref = blocked[logical_id]
            results.append(
                HistoricalReplayCaseResult(
                    logical_signal_id=logical_id,
                    symbol=request.armed.symbol,
                    confirmation_close=request.armed.confirmation_close,
                    status=HistoricalReplayStatus.BLOCKED,
                    reason_codes=reasons,
                    rule_source_ref=source_ref,
                    replay=None,
                )
            )
            continue
        replay = replayed[logical_id]
        status = (
            HistoricalReplayStatus.EXECUTED
            if replay.status is ReplayCaseStatus.EXECUTED
            else HistoricalReplayStatus.SKIPPED_ACTIVE_TRADE
        )
        rule = _active_rule(registry, request.armed.symbol, request.armed.confirmation_close)
        results.append(
            HistoricalReplayCaseResult(
                logical_signal_id=logical_id,
                symbol=request.armed.symbol,
                confirmation_close=request.armed.confirmation_close,
                status=status,
                reason_codes=(),
                rule_source_ref=None if rule is None else rule.source_ref,
                replay=replay,
            )
        )
    payload = {
        "universe_version": universe.universe_version,
        "contract_registry_version": registry.registry_version,
        "cases": [result.model_dump(mode="json") for result in results],
    }
    return HistoricalReplayResult(
        universe_version=universe.universe_version,
        contract_registry_version=registry.registry_version,
        cases=tuple(results),
        result_hash=_canonical_sha256(payload),
    )


def write_historical_rule_gate(
    report: HistoricalRuleGateReport,
    data_dir: Path,
) -> Path:
    """Persist a content-addressed rule-gate report atomically."""

    destination = (
        data_dir / "manifests" / "historical_rule_gate" / f"{report.report_hash}.json"
    )
    content = (
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise HistoricalReplayError(
                f"existing content-addressed gate report changed: {destination}"
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
