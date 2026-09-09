"""Audit post-lifecycle indicator warm-up without authorizing history seeds."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

import pandas as pd
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.data.archive import sha256_file
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.candle_inputs import (
    CandlePartitionSource,
    contained_file,
    load_verified_candle_partition,
)
from slagalpha.research.lifecycle_real_execution import LifecycleRealActionResult
from slagalpha.research.lifecycle_remediation import LifecycleIntervalRemediationAction
from slagalpha.research.lifecycle_replacement import LifecycleReplacementNormalizationResult

Interval = Literal["15m", "1h", "4h", "1d"]
WarmupStatus = Literal["SUFFICIENT", "BLOCKED"]

_INTERVAL = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
_IDENTITY = re.compile(r"^[A-Z0-9]+USDT/(15m|1h|4h|1d)$")
_SMA_WARMUP_BARS = 180
_ATR_WARMUP_BARS = 14


class AtrHistoryAuditError(ValueError):
    """Raised when lifecycle warm-up evidence cannot be interpreted safely."""


class LifecycleWarmupInput(BaseModel):
    """Evidence supplied by a caller for one lifecycle/interval stream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    lifecycle_start: datetime
    warmup_prefix_start: datetime | None = None
    verified_post_cutoff_row_count: int = Field(ge=0)
    complete_same_lifecycle_prefix: bool
    contiguous_same_lifecycle_prefix: bool
    source_reference: str

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if _IDENTITY.fullmatch(self.identity) is None:
            raise ValueError("warm-up identity must be SYMBOL/interval")
        if self.lifecycle_start.tzinfo is None or self.lifecycle_start.utcoffset() != timedelta(0):
            raise ValueError("lifecycle start must use UTC")
        if self.lifecycle_start.microsecond:
            raise ValueError("lifecycle start must use exact seconds")
        prefix_start = self.warmup_prefix_start
        if prefix_start is not None and (
            prefix_start.tzinfo is None
            or prefix_start.utcoffset() != timedelta(0)
            or prefix_start.microsecond
            or prefix_start < self.lifecycle_start
        ):
            raise ValueError("warm-up prefix start must be an exact UTC time after lifecycle start")
        if len(self.source_reference) != 64 or any(
            c not in "0123456789abcdef" for c in self.source_reference
        ):
            raise ValueError("warm-up source reference must be lowercase SHA-256")
        return self


class LifecycleWarmupEvidence(BaseModel):
    """Decision boundary for one stream; no field grants seed or research permission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    lifecycle_start: datetime
    warmup_prefix_start: datetime | None = None
    required_sma_bars: int = _SMA_WARMUP_BARS
    required_atr_bars: int = _ATR_WARMUP_BARS
    required_warmup_bars: int = _SMA_WARMUP_BARS
    first_usable_open_time: datetime
    verified_post_cutoff_row_count: int = Field(ge=0)
    complete_same_lifecycle_prefix: bool
    contiguous_same_lifecycle_prefix: bool
    source_reference: str
    status: WarmupStatus
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        match = _IDENTITY.fullmatch(self.identity)
        if match is None:
            raise ValueError("warm-up identity must be SYMBOL/interval")
        interval = match.group(1)
        if self.lifecycle_start.tzinfo is None or self.lifecycle_start.utcoffset() != timedelta(0):
            raise ValueError("lifecycle start must use UTC")
        prefix_start = self.warmup_prefix_start or self.lifecycle_start
        if self.warmup_prefix_start is not None and (
            int(prefix_start.timestamp()) % int(_INTERVAL[interval].total_seconds())
        ):
            raise ValueError("warm-up prefix start must align to the interval grid")
        expected_first = prefix_start + _INTERVAL[interval] * self.required_warmup_bars
        if self.first_usable_open_time != expected_first:
            raise ValueError("first usable time does not match warm-up boundary")
        if self.required_sma_bars != _SMA_WARMUP_BARS or self.required_atr_bars != _ATR_WARMUP_BARS:
            raise ValueError("indicator warm-up requirements are frozen")
        if self.required_warmup_bars != max(self.required_sma_bars, self.required_atr_bars):
            raise ValueError("warm-up bars must cover SMA and ATR")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("warm-up reason codes must be unique and canonical")
        if self.status == "SUFFICIENT" and self.reason_codes:
            raise ValueError("sufficient warm-up evidence cannot have reason codes")
        if self.status == "SUFFICIENT" and (
            not self.complete_same_lifecycle_prefix
            or not self.contiguous_same_lifecycle_prefix
            or self.verified_post_cutoff_row_count < self.required_warmup_bars
        ):
            raise ValueError("sufficient warm-up evidence is incomplete")
        if self.status == "BLOCKED" and not self.reason_codes:
            raise ValueError("blocked warm-up evidence requires reason codes")
        return self


class AtrHistorySeedAuditReport(BaseModel):
    """Content-addressed lifecycle warm-up audit, never an authorization receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[
        "atr-history-seed-audit/0.1.0", "atr-history-seed-audit/0.2.0"
    ] = "atr-history-seed-audit/0.2.0"
    replacement_result_hash: str
    replacement_dataset_content_hash: str
    required_sma_bars: int = _SMA_WARMUP_BARS
    required_atr_bars: int = _ATR_WARMUP_BARS
    streams: tuple[LifecycleWarmupEvidence, ...]
    unresolved_lifecycle_identities: tuple[str, ...]
    sufficient_stream_count: int = Field(ge=0)
    blocked_stream_count: int = Field(ge=0)
    status: Literal["BLOCKED", "CHECKS_PASSED"]
    blockers: tuple[str, ...]
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        for value in (
            self.replacement_result_hash,
            self.replacement_dataset_content_hash,
            self.report_hash,
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("ATR audit references must be lowercase SHA-256")
        identities = tuple(item.identity for item in self.streams)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("ATR audit streams must be unique and canonical")
        if self.unresolved_lifecycle_identities != tuple(
            sorted(set(self.unresolved_lifecycle_identities))
        ):
            raise ValueError("unresolved lifecycle identities must be unique and canonical")
        if self.sufficient_stream_count != sum(
            item.status == "SUFFICIENT" for item in self.streams
        ):
            raise ValueError("sufficient stream count does not reconcile")
        if self.blocked_stream_count != sum(item.status == "BLOCKED" for item in self.streams):
            raise ValueError("blocked stream count does not reconcile")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("ATR audit blockers must be unique and canonical")
        if (self.status == "BLOCKED") != bool(self.blockers):
            raise ValueError("ATR audit status and blockers disagree")
        if self.status == "CHECKS_PASSED" and self.unresolved_lifecycle_identities:
            raise ValueError("unresolved lifecycle identities must block")
        payload = self.model_dump(mode="json", exclude={"report_hash"}, exclude_none=True)
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("ATR audit content hash mismatch")
        return self


def _action_identities(replacement: LifecycleReplacementNormalizationResult) -> tuple[str, ...]:
    partitions = (
        set(replacement.resolved_failure_identities)
        | set(replacement.shadowed_daily_partition_identities)
    )
    return tuple(sorted("/".join(identity.split("/")[:2]) for identity in partitions))


def _continuous_prefix_count(
    open_times: Iterable[datetime], *, start: datetime, interval: Interval
) -> tuple[int, bool]:
    """Count the exact interval-grid prefix and report whether all supplied rows belong to it."""

    expected = start
    count = 0
    complete_input = True
    for value in open_times:
        observed = pd.Timestamp(value).to_pydatetime()
        if observed != expected:
            complete_input = False
            break
        count += 1
        expected += _INTERVAL[interval]
    return count, complete_input


def load_verified_lifecycle_warmup_input(
    *,
    project_dir: Path,
    action: LifecycleIntervalRemediationAction,
    output: LifecycleRealActionResult,
    subsequent_sources: tuple[CandlePartitionSource, ...],
) -> LifecycleWarmupInput:
    """Revalidate a boundary derivative and its exact post-boundary monthly continuation."""

    action = LifecycleIntervalRemediationAction.model_validate(action.model_dump(mode="json"))
    output = LifecycleRealActionResult.model_validate(output.model_dump(mode="json"))
    sources = tuple(
        CandlePartitionSource.model_validate(item.model_dump(mode="json"))
        for item in subsequent_sources
    )
    action_hash = hashlib.sha256(canonical_json_bytes(action.model_dump(mode="json"))).hexdigest()
    if (
        (output.symbol, output.interval, output.evidence_period, output.action_hash)
        != (action.symbol, action.interval, action.evidence_period, action_hash)
        or output.retained_row_count != action.boundary_month_expected_retained_row_count
    ):
        raise AtrHistoryAuditError("lifecycle output does not match its remediation action")

    evidence_path = contained_file(project_dir, output.output_relative_path)
    output_root = (project_dir / "data/normalized/lifecycle_scoped/v0.1.0").resolve()
    if not evidence_path.resolve().is_relative_to(output_root):
        raise AtrHistoryAuditError("lifecycle derivative escapes its dedicated namespace")
    if sha256_file(evidence_path) != output.output_sha256:
        raise AtrHistoryAuditError("lifecycle derivative output hash changed")
    open_times: list[datetime] = []
    if output.status == "MATERIALIZED":
        try:
            table = pq.ParquetFile(evidence_path).read(columns=["open_time"])
            boundary_times = table.column("open_time").to_pylist()
        except (OSError, ValueError) as error:
            raise AtrHistoryAuditError("lifecycle derivative timestamps are unreadable") from error
        if len(boundary_times) != output.retained_row_count:
            raise AtrHistoryAuditError("lifecycle derivative row count changed")
        open_times.extend(boundary_times)
    elif output.retained_row_count:
        raise AtrHistoryAuditError("empty lifecycle exclusion cannot retain rows")

    year, month = (int(part) for part in action.evidence_period.split("-"))
    expected_year, expected_month = (year + 1, 1) if month == 12 else (year, month + 1)
    for source in sources:
        expected_period = f"{expected_year:04d}-{expected_month:02d}"
        if (
            source.spec.symbol != action.symbol
            or source.spec.interval != action.interval
            or source.spec.period != expected_period
        ):
            raise AtrHistoryAuditError("post-boundary sources must be consecutive canonical months")
        frame = load_verified_candle_partition(project_dir=project_dir, source=source)
        open_times.extend(frame["open_time"].dt.to_pydatetime())
        expected_year, expected_month = (
            (expected_year + 1, 1) if expected_month == 12 else (expected_year, expected_month + 1)
        )

    prefix_count, all_rows_contiguous = _continuous_prefix_count(
        open_times, start=action.retain_from_open_time, interval=action.interval
    )
    reference_payload = {
        "action": action.model_dump(mode="json"),
        "output": output.model_dump(mode="json"),
        "subsequent_sources": [item.model_dump(mode="json") for item in sources],
    }
    return LifecycleWarmupInput(
        identity=f"{action.symbol}/{action.interval}",
        lifecycle_start=action.identity_effective_from,
        warmup_prefix_start=action.retain_from_open_time,
        verified_post_cutoff_row_count=prefix_count,
        complete_same_lifecycle_prefix=prefix_count >= _SMA_WARMUP_BARS,
        contiguous_same_lifecycle_prefix=all_rows_contiguous,
        source_reference=hashlib.sha256(canonical_json_bytes(reference_payload)).hexdigest(),
    )


def build_atr_history_seed_audit(
    *,
    replacement: LifecycleReplacementNormalizationResult,
    expected_replacement_hash: str,
    inputs: tuple[LifecycleWarmupInput, ...],
    unresolved_lifecycle_identities: tuple[str, ...] = (),
) -> AtrHistorySeedAuditReport:
    """Evaluate only explicit post-cutoff evidence; old lifecycle data is never accepted."""

    replacement = LifecycleReplacementNormalizationResult.model_validate(
        replacement.model_dump(mode="json")
    )
    if replacement.result_hash != expected_replacement_hash:
        raise AtrHistoryAuditError("unexpected lifecycle replacement result")
    ordered_inputs = tuple(sorted(
        (LifecycleWarmupInput.model_validate(item.model_dump(mode="json")) for item in inputs),
        key=lambda item: item.identity,
    ))
    expected = _action_identities(replacement)
    observed = tuple(item.identity for item in ordered_inputs)
    if observed != tuple(sorted(set(observed))):
        raise AtrHistoryAuditError("warm-up inputs must be unique and canonical")
    if observed != expected:
        raise AtrHistoryAuditError("warm-up inputs must cover replacement lifecycle actions")
    unresolved = tuple(sorted(set(unresolved_lifecycle_identities)))
    if unresolved != unresolved_lifecycle_identities:
        raise AtrHistoryAuditError("unresolved lifecycle identities must be canonical")

    evidence: list[LifecycleWarmupEvidence] = []
    for item in ordered_inputs:
        interval = item.identity.rsplit("/", 1)[1]
        reasons: list[str] = []
        if not item.complete_same_lifecycle_prefix:
            reasons.append("SAME_LIFECYCLE_PREFIX_NOT_PROVEN")
        if not item.contiguous_same_lifecycle_prefix:
            reasons.append("SAME_LIFECYCLE_CONTINUITY_NOT_PROVEN")
        if item.verified_post_cutoff_row_count < _SMA_WARMUP_BARS:
            reasons.append("INSUFFICIENT_POST_CUTOFF_WARMUP")
        if item.verified_post_cutoff_row_count == 0:
            reasons.append("NO_POST_CUTOFF_ROWS_VERIFIED")
        status: WarmupStatus = "SUFFICIENT" if not reasons else "BLOCKED"
        evidence.append(LifecycleWarmupEvidence(
            identity=item.identity,
            lifecycle_start=item.lifecycle_start,
            warmup_prefix_start=item.warmup_prefix_start,
            first_usable_open_time=(item.warmup_prefix_start or item.lifecycle_start)
            + _INTERVAL[interval] * _SMA_WARMUP_BARS,
            verified_post_cutoff_row_count=item.verified_post_cutoff_row_count,
            complete_same_lifecycle_prefix=item.complete_same_lifecycle_prefix,
            contiguous_same_lifecycle_prefix=item.contiguous_same_lifecycle_prefix,
            source_reference=item.source_reference,
            status=status,
            reason_codes=tuple(sorted(set(reasons))),
        ))
    blockers = {
        *(f"UNRESOLVED_LIFECYCLE_IDENTITY:{identity}" for identity in unresolved),
    }
    if any(item.status == "BLOCKED" for item in evidence):
        blockers.add("ATR_HISTORY_WARMUP_NOT_PROVEN_AFTER_CUTOFF")
    ordered_evidence = tuple(evidence)
    payload: dict[str, Any] = {
        "schema_version": "atr-history-seed-audit/0.2.0",
        "replacement_result_hash": replacement.result_hash,
        "replacement_dataset_content_hash": replacement.replacement_dataset_content_hash,
        "required_sma_bars": _SMA_WARMUP_BARS,
        "required_atr_bars": _ATR_WARMUP_BARS,
        "streams": [item.model_dump(mode="json", exclude_none=True) for item in ordered_evidence],
        "unresolved_lifecycle_identities": list(unresolved),
        "sufficient_stream_count": sum(item.status == "SUFFICIENT" for item in ordered_evidence),
        "blocked_stream_count": sum(item.status == "BLOCKED" for item in ordered_evidence),
        "status": "BLOCKED" if blockers else "CHECKS_PASSED",
        "blockers": sorted(blockers),
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    return AtrHistorySeedAuditReport.model_validate({
        **payload,
        "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def write_atr_history_seed_audit(report: AtrHistorySeedAuditReport, data_dir: Path) -> Path:
    report = AtrHistorySeedAuditReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests" / "atr_history_seed_audit" / f"{report.report_hash}.json"
    content = canonical_json_bytes(report.model_dump(mode="json", exclude_none=True))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise AtrHistoryAuditError("existing ATR audit changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
