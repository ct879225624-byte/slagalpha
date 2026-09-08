"""Audit relisted contract lifecycle boundaries from immutable local archives."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.domain.universe import (
    ContractIdentityLifecycleEntry,
    ContractIdentityRegistry,
)
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.normalization_gaps import GapSpan

IntradayInterval = Literal["15m", "1h", "4h"]
ArchiveInterval = Literal["15m", "1h", "4h", "1d"]

_FAILURE_IDENTITY = re.compile(
    r"^(?P<symbol>[A-Z0-9]+USDT)/(?P<interval>15m|1h|4h)/(?P<period>\d{4}-\d{2})$"
)
_SYMBOL = re.compile(r"^[A-Z0-9]+USDT$")
_INTRADAY_INTERVALS: tuple[IntradayInterval, ...] = ("15m", "1h", "4h")
_INTERVAL_DELTA: dict[ArchiveInterval, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


class LifecycleBoundaryAuditError(ValueError):
    """Raised when lifecycle evidence cannot be interpreted safely."""


def _require_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("lifecycle audit references must be lowercase SHA-256")
    return value


class ArchiveFingerprint(BaseModel):
    """Content and time-grid evidence for one raw monthly ZIP."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    interval: ArchiveInterval
    period: str
    source_sha256: str
    rows_sha256: str
    row_count: int = Field(gt=0)
    first_open_time: datetime
    last_open_time: datetime
    gaps: tuple[GapSpan, ...]

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        _require_sha256(self.source_sha256)
        _require_sha256(self.rows_sha256)
        if self.first_open_time > self.last_open_time:
            raise ValueError("archive first open time must not follow its last open time")
        return self


class IntradayLifecycleBoundary(BaseModel):
    """One intraday gap compared with the corresponding settled archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval: IntradayInterval
    expected_boundary_open_time: datetime | None
    primary_archive: ArchiveFingerprint | None
    settled_archive: ArchiveFingerprint | None
    shared_open_time_count: int = Field(ge=0)
    conflicting_shared_open_time_count: int = Field(ge=0)
    status: Literal["CONFIRMED_BOUNDARY", "UNRESOLVED"]
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("intraday reason codes must be unique and canonical")
        if self.conflicting_shared_open_time_count > self.shared_open_time_count:
            raise ValueError("conflicting shared row count exceeds shared row count")
        if (self.status == "CONFIRMED_BOUNDARY") == bool(self.reason_codes):
            raise ValueError("intraday status and reason codes disagree")
        if self.status == "CONFIRMED_BOUNDARY" and (
            self.expected_boundary_open_time is None
            or self.primary_archive is None
            or self.settled_archive is None
        ):
            raise ValueError("confirmed boundary requires complete archive and identity evidence")
        return self


class DailyLifecycleBoundary(BaseModel):
    """Separate daily-candle check for cross-lifecycle primary history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_boundary_open_time: datetime | None
    primary_archive: ArchiveFingerprint | None
    settled_archive: ArchiveFingerprint | None
    primary_rows_before_boundary: int = Field(ge=0)
    primary_rows_at_or_after_boundary: int = Field(ge=0)
    shared_open_time_count: int = Field(ge=0)
    conflicting_shared_open_time_count: int = Field(ge=0)
    status: Literal["ISOLATED", "MIXED_OLD_LIFECYCLE", "UNRESOLVED"]
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("daily reason codes must be unique and canonical")
        if self.conflicting_shared_open_time_count > self.shared_open_time_count:
            raise ValueError("conflicting shared daily row count exceeds shared row count")
        contamination = "PRIMARY_ARCHIVE_CONTAINS_PRE_BOUNDARY_ROWS"
        if self.status == "ISOLATED" and self.reason_codes:
            raise ValueError("isolated daily evidence must not have reason codes")
        if self.status == "MIXED_OLD_LIFECYCLE" and self.reason_codes != (contamination,):
            raise ValueError("mixed daily evidence must identify pre-boundary rows")
        if self.status != "UNRESOLVED" and (
            self.expected_boundary_open_time is None
            or self.primary_archive is None
            or self.settled_archive is None
        ):
            raise ValueError("resolved daily evidence requires complete archives and identity")
        return self


class SymbolLifecycleBoundaryAudit(BaseModel):
    """All evidence for one primary symbol and its settled predecessor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    settled_symbol: str
    period: str | None
    identity_effective_from: datetime | None
    identity_source_ref: str | None
    intraday: tuple[IntradayLifecycleBoundary, ...]
    daily: DailyLifecycleBoundary
    status: Literal["CONFIRMED_RELIST_BOUNDARY", "UNRESOLVED"]
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_symbol_audit(self) -> Self:
        if self.settled_symbol != f"{self.symbol}SETTLED":
            raise ValueError("settled symbol must be the exact primary symbol plus SETTLED")
        if tuple(item.interval for item in self.intraday) != _INTRADAY_INTERVALS:
            raise ValueError("intraday evidence must use canonical 15m/1h/4h order")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("symbol reason codes must be unique and canonical")
        resolved = (
            self.period is not None
            and self.identity_effective_from is not None
            and self.identity_source_ref is not None
            and all(item.status == "CONFIRMED_BOUNDARY" for item in self.intraday)
            and self.daily.status != "UNRESOLVED"
        )
        if (self.status == "CONFIRMED_RELIST_BOUNDARY") != resolved:
            raise ValueError("symbol lifecycle status does not reconcile with its evidence")
        if (self.status == "UNRESOLVED") != bool(self.reason_codes):
            raise ValueError("unresolved symbol must have canonical reason codes")
        return self


class LifecycleBoundaryAuditReport(BaseModel):
    """Content-addressed, non-authorizing relist lifecycle audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-boundary-audit/0.1.0"] = (
        "lifecycle-boundary-audit/0.1.0"
    )
    normalization_result_hash: str
    identity_registry_hash: str
    target_count: int = Field(gt=0)
    confirmed_relist_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    daily_mixed_lifecycle_count: int = Field(ge=0)
    symbols: tuple[SymbolLifecycleBoundaryAudit, ...]
    status: Literal["AUDITED", "BLOCKED"]
    blockers: tuple[str, ...]
    normalization_result_mutation_authorized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        for value in (
            self.normalization_result_hash,
            self.identity_registry_hash,
            self.report_hash,
        ):
            _require_sha256(value)
        names = tuple(item.symbol for item in self.symbols)
        if names != tuple(sorted(set(names))) or self.target_count != len(names):
            raise ValueError("audit targets must be unique, canonical, and reconciled")
        if self.confirmed_relist_count != sum(
            item.status == "CONFIRMED_RELIST_BOUNDARY" for item in self.symbols
        ):
            raise ValueError("confirmed relist count does not reconcile")
        if self.unresolved_count != sum(
            item.status == "UNRESOLVED" for item in self.symbols
        ):
            raise ValueError("unresolved lifecycle count does not reconcile")
        if self.daily_mixed_lifecycle_count != sum(
            item.daily.status == "MIXED_OLD_LIFECYCLE" for item in self.symbols
        ):
            raise ValueError("daily mixed lifecycle count does not reconcile")
        if self.confirmed_relist_count + self.unresolved_count != self.target_count:
            raise ValueError("lifecycle status counts do not cover every target")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("lifecycle blockers must be unique and canonical")
        if (self.status == "BLOCKED") != bool(self.blockers):
            raise ValueError("lifecycle audit status and blockers disagree")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("lifecycle audit content hash mismatch")
        return self


@dataclass(frozen=True)
class _ArchiveRows:
    fingerprint: ArchiveFingerprint
    rows_by_open_time: dict[datetime, tuple[str, ...]]


def _month_bounds(period: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as error:
        raise LifecycleBoundaryAuditError("invalid lifecycle audit period") from error
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


def _floor_to_interval(value: datetime, interval: ArchiveInterval) -> datetime:
    step_ms = int(_INTERVAL_DELTA[interval].total_seconds() * 1000)
    value_ms = int(value.timestamp() * 1000)
    return datetime.fromtimestamp((value_ms - value_ms % step_ms) / 1000, UTC)


def _archive_path(
    root: Path, symbol: str, interval: ArchiveInterval, period: str
) -> Path:
    return root / symbol / interval / f"{symbol}-{interval}-{period}.zip"


def _read_archive(
    path: Path, *, symbol: str, interval: ArchiveInterval, period: str
) -> _ArchiveRows:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        with zipfile.ZipFile(path) as archive:
            members = tuple(info for info in archive.infolist() if not info.is_dir())
            expected_member = f"{symbol}-{interval}-{period}.csv"
            if len(members) != 1 or members[0].filename != expected_member:
                raise LifecycleBoundaryAuditError(
                    "monthly lifecycle archive must contain its exact canonical CSV"
                )
            with archive.open(members[0]) as raw:
                rows = list(csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")))
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise LifecycleBoundaryAuditError("monthly lifecycle archive cannot be read") from error
    if rows and rows[0] and rows[0][0] == "open_time":
        rows = rows[1:]
    try:
        parsed_rows = tuple((int(row[0]), tuple(row)) for row in rows if row)
    except (IndexError, ValueError) as error:
        raise LifecycleBoundaryAuditError(
            "monthly lifecycle archive has an invalid open time"
        ) from error
    if not parsed_rows:
        raise LifecycleBoundaryAuditError("monthly lifecycle archive has no candle rows")
    open_times_ms = tuple(item[0] for item in parsed_rows)
    if any(
        current <= previous
        for previous, current in zip(open_times_ms, open_times_ms[1:], strict=False)
    ):
        raise LifecycleBoundaryAuditError(
            "monthly lifecycle archive open times must strictly increase"
        )
    month_start, month_end = _month_bounds(period)
    step_ms = int(_INTERVAL_DELTA[interval].total_seconds() * 1000)
    if any(
        value < int(month_start.timestamp() * 1000)
        or value >= int(month_end.timestamp() * 1000)
        or value % step_ms
        for value in open_times_ms
    ):
        raise LifecycleBoundaryAuditError(
            "monthly lifecycle archive open times are outside grid or month"
        )
    open_times = tuple(datetime.fromtimestamp(value / 1000, UTC) for value in open_times_ms)
    gaps = tuple(
        GapSpan(
            row_position=index,
            before_open_time=previous_time,
            after_open_time=current_time,
            delta_ms=int((current_time - previous_time).total_seconds() * 1000),
        )
        for index, (previous_time, current_time) in enumerate(
            zip(open_times, open_times[1:], strict=False), start=1
        )
        if current_time - previous_time != _INTERVAL_DELTA[interval]
    )
    rows_payload = {"rows": [list(row) for _, row in parsed_rows]}
    fingerprint = ArchiveFingerprint(
        symbol=symbol,
        interval=interval,
        period=period,
        source_sha256=digest.hexdigest(),
        rows_sha256=hashlib.sha256(canonical_json_bytes(rows_payload)).hexdigest(),
        row_count=len(parsed_rows),
        first_open_time=open_times[0],
        last_open_time=open_times[-1],
        gaps=gaps,
    )
    return _ArchiveRows(
        fingerprint=fingerprint,
        rows_by_open_time={
            open_time: row for open_time, (_, row) in zip(open_times, parsed_rows, strict=True)
        },
    )


def _shared_row_counts(
    primary: _ArchiveRows, settled: _ArchiveRows
) -> tuple[int, int, tuple[datetime, ...]]:
    shared = tuple(sorted(primary.rows_by_open_time.keys() & settled.rows_by_open_time.keys()))
    conflicts = sum(
        primary.rows_by_open_time[open_time] != settled.rows_by_open_time[open_time]
        for open_time in shared
    )
    return len(shared), conflicts, shared


def _audit_intraday(
    *,
    root: Path,
    symbol: str,
    interval: IntradayInterval,
    period: str,
    effective_from: datetime | None,
    failure_error: str | None,
) -> IntradayLifecycleBoundary:
    reasons: list[str] = []
    primary: _ArchiveRows | None = None
    settled: _ArchiveRows | None = None
    try:
        primary = _read_archive(
            _archive_path(root, symbol, interval, period),
            symbol=symbol,
            interval=interval,
            period=period,
        )
    except LifecycleBoundaryAuditError:
        reasons.append("PRIMARY_ARCHIVE_UNAVAILABLE_OR_INVALID")
    settled_symbol = f"{symbol}SETTLED"
    try:
        settled = _read_archive(
            _archive_path(root, settled_symbol, interval, period),
            symbol=settled_symbol,
            interval=interval,
            period=period,
        )
    except LifecycleBoundaryAuditError:
        reasons.append("SETTLED_ARCHIVE_UNAVAILABLE_OR_INVALID")
    boundary = None if effective_from is None else _floor_to_interval(effective_from, interval)
    if boundary is None:
        reasons.append("VERIFIED_IDENTITY_EFFECTIVE_FROM_UNAVAILABLE")
    if failure_error is None or not failure_error.startswith("CandleGapError:"):
        reasons.append("PRIMARY_NORMALIZATION_FAILURE_NOT_CANDLE_GAP")
    shared_count = 0
    conflict_count = 0
    if primary is not None:
        if len(primary.fingerprint.gaps) != 1:
            reasons.append("PRIMARY_ARCHIVE_EXPECTED_SINGLE_GAP")
    if settled is not None and settled.fingerprint.gaps:
        reasons.append("SETTLED_ARCHIVE_NOT_CONTIGUOUS")
    if primary is not None and settled is not None:
        shared_count, conflict_count, shared = _shared_row_counts(primary, settled)
        if boundary is not None and any(open_time != boundary for open_time in shared):
            reasons.append("SETTLED_OVERLAP_OUTSIDE_RELIST_BUCKET")
        if conflict_count != shared_count:
            reasons.append("OVERLAPPING_LIFECYCLE_ROWS_NOT_DISTINCT")
    if primary is not None and settled is not None and len(primary.fingerprint.gaps) == 1:
        gap = primary.fingerprint.gaps[0]
        if boundary is not None and gap.after_open_time != boundary:
            reasons.append("PRIMARY_GAP_AFTER_IDENTITY_BOUNDARY_MISMATCH")
        if boundary is not None and gap.before_open_time >= boundary:
            reasons.append("PRIMARY_GAP_DOES_NOT_START_BEFORE_IDENTITY_BOUNDARY")
        if (
            settled.fingerprint.first_open_time <= gap.before_open_time
            or settled.fingerprint.last_open_time > gap.after_open_time
        ):
            reasons.append("SETTLED_ARCHIVE_ROWS_OUTSIDE_PRIMARY_GAP")
    canonical_reasons = tuple(sorted(set(reasons)))
    return IntradayLifecycleBoundary(
        interval=interval,
        expected_boundary_open_time=boundary,
        primary_archive=None if primary is None else primary.fingerprint,
        settled_archive=None if settled is None else settled.fingerprint,
        shared_open_time_count=shared_count,
        conflicting_shared_open_time_count=conflict_count,
        status="UNRESOLVED" if canonical_reasons else "CONFIRMED_BOUNDARY",
        reason_codes=canonical_reasons,
    )


def _audit_daily(
    *, root: Path, symbol: str, period: str, effective_from: datetime | None
) -> DailyLifecycleBoundary:
    reasons: list[str] = []
    primary: _ArchiveRows | None = None
    settled: _ArchiveRows | None = None
    try:
        primary = _read_archive(
            _archive_path(root, symbol, "1d", period),
            symbol=symbol,
            interval="1d",
            period=period,
        )
    except LifecycleBoundaryAuditError:
        reasons.append("PRIMARY_DAILY_ARCHIVE_UNAVAILABLE_OR_INVALID")
    settled_symbol = f"{symbol}SETTLED"
    try:
        settled = _read_archive(
            _archive_path(root, settled_symbol, "1d", period),
            symbol=settled_symbol,
            interval="1d",
            period=period,
        )
    except LifecycleBoundaryAuditError:
        reasons.append("SETTLED_DAILY_ARCHIVE_UNAVAILABLE_OR_INVALID")
    boundary = None if effective_from is None else _floor_to_interval(effective_from, "1d")
    if boundary is None:
        reasons.append("VERIFIED_IDENTITY_EFFECTIVE_FROM_UNAVAILABLE")
    before_count = 0
    after_count = 0
    shared_count = 0
    conflict_count = 0
    if primary is not None:
        if primary.fingerprint.gaps:
            reasons.append("PRIMARY_DAILY_ARCHIVE_NOT_CONTIGUOUS")
        if boundary is not None:
            before_count = sum(
                open_time < boundary for open_time in primary.rows_by_open_time
            )
            after_count = sum(
                open_time >= boundary for open_time in primary.rows_by_open_time
            )
            if not after_count:
                reasons.append("PRIMARY_DAILY_ARCHIVE_HAS_NO_NEW_LIFECYCLE_ROWS")
    if settled is not None:
        if settled.fingerprint.gaps:
            reasons.append("SETTLED_DAILY_ARCHIVE_NOT_CONTIGUOUS")
        if boundary is not None and set(settled.rows_by_open_time) != {boundary}:
            reasons.append("SETTLED_DAILY_ARCHIVE_NOT_LIMITED_TO_BOUNDARY_BUCKET")
    if primary is not None and settled is not None:
        shared_count, conflict_count, shared = _shared_row_counts(primary, settled)
        if boundary is not None and shared != (boundary,):
            reasons.append("DAILY_ARCHIVES_DO_NOT_SHARE_EXACT_BOUNDARY_BUCKET")
        if conflict_count != shared_count or conflict_count != 1:
            reasons.append("DAILY_BOUNDARY_LIFECYCLE_ROWS_NOT_DISTINCT")
    evidence_reasons = tuple(sorted(set(reasons)))
    if evidence_reasons:
        status: Literal["ISOLATED", "MIXED_OLD_LIFECYCLE", "UNRESOLVED"] = "UNRESOLVED"
        reason_codes = evidence_reasons
    elif before_count:
        status = "MIXED_OLD_LIFECYCLE"
        reason_codes = ("PRIMARY_ARCHIVE_CONTAINS_PRE_BOUNDARY_ROWS",)
    else:
        status = "ISOLATED"
        reason_codes = ()
    return DailyLifecycleBoundary(
        expected_boundary_open_time=boundary,
        primary_archive=None if primary is None else primary.fingerprint,
        settled_archive=None if settled is None else settled.fingerprint,
        primary_rows_before_boundary=before_count,
        primary_rows_at_or_after_boundary=after_count,
        shared_open_time_count=shared_count,
        conflicting_shared_open_time_count=conflict_count,
        status=status,
        reason_codes=reason_codes,
    )


def _unresolved_without_period(
    symbol: str, reason_codes: tuple[str, ...]
) -> SymbolLifecycleBoundaryAudit:
    intraday = tuple(
        IntradayLifecycleBoundary(
            interval=interval,
            expected_boundary_open_time=None,
            primary_archive=None,
            settled_archive=None,
            shared_open_time_count=0,
            conflicting_shared_open_time_count=0,
            status="UNRESOLVED",
            reason_codes=reason_codes,
        )
        for interval in _INTRADAY_INTERVALS
    )
    return SymbolLifecycleBoundaryAudit(
        symbol=symbol,
        settled_symbol=f"{symbol}SETTLED",
        period=None,
        identity_effective_from=None,
        identity_source_ref=None,
        intraday=intraday,
        daily=DailyLifecycleBoundary(
            expected_boundary_open_time=None,
            primary_archive=None,
            settled_archive=None,
            primary_rows_before_boundary=0,
            primary_rows_at_or_after_boundary=0,
            shared_open_time_count=0,
            conflicting_shared_open_time_count=0,
            status="UNRESOLVED",
            reason_codes=reason_codes,
        ),
        status="UNRESOLVED",
        reason_codes=reason_codes,
    )


def _identity_for_period(
    registry: ContractIdentityRegistry, symbol: str, period: str
) -> tuple[ContractIdentityLifecycleEntry | None, str | None]:
    month_start, month_end = _month_bounds(period)
    matches = tuple(
        entry
        for entry in registry.entries
        if entry.symbol == symbol
        and entry.eligible_for_locked_research
        and month_start <= entry.effective_from < month_end
    )
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, "VERIFIED_IDENTITY_EFFECTIVE_FROM_UNAVAILABLE"
    return None, "VERIFIED_IDENTITY_EFFECTIVE_FROM_NOT_UNIQUE"


def build_lifecycle_boundary_audit(
    *,
    normalization: NormalizationBatchResult,
    identity_registry: ContractIdentityRegistry,
    target_symbols: tuple[str, ...],
    monthly_klines_dir: Path,
) -> LifecycleBoundaryAuditReport:
    """Compare relist gaps and daily archives without authorizing any data mutation."""

    normalization = NormalizationBatchResult.model_validate(
        normalization.model_dump(mode="json")
    )
    identity_registry = ContractIdentityRegistry.model_validate(
        identity_registry.model_dump(mode="json")
    )
    targets = tuple(sorted(set(target_symbols)))
    if not targets or targets != target_symbols or any(
        _SYMBOL.fullmatch(symbol) is None for symbol in targets
    ):
        raise LifecycleBoundaryAuditError(
            "target symbols must be non-empty, unique, canonical USDT symbols"
        )
    registry_prefix = "identity-registry-"
    if not identity_registry.registry_version.startswith(registry_prefix):
        raise LifecycleBoundaryAuditError("identity registry version is not content addressed")
    identity_registry_hash = identity_registry.registry_version.removeprefix(registry_prefix)
    try:
        _require_sha256(identity_registry_hash)
    except ValueError as error:
        raise LifecycleBoundaryAuditError("identity registry hash is invalid") from error

    failures: dict[str, list[tuple[IntradayInterval, str, str]]] = {
        symbol: [] for symbol in targets
    }
    for failure in normalization.failures:
        match = _FAILURE_IDENTITY.fullmatch(failure.identity)
        if match is None or match.group("symbol") not in failures:
            continue
        failures[match.group("symbol")].append(
            (
                cast(IntradayInterval, match.group("interval")),
                match.group("period"),
                failure.error,
            )
        )

    symbol_audits: list[SymbolLifecycleBoundaryAudit] = []
    blockers: list[str] = []
    for symbol in targets:
        records = failures[symbol]
        periods = {period for _, period, _ in records}
        intervals = tuple(sorted(interval for interval, _, _ in records))
        if len(records) != 3 or len(periods) != 1 or set(intervals) != set(_INTRADAY_INTERVALS):
            reason_codes = ("NORMALIZATION_FAILURE_TRIPLET_UNAVAILABLE",)
            audit = _unresolved_without_period(symbol, reason_codes)
            symbol_audits.append(audit)
            blockers.append(f"LIFECYCLE_BOUNDARY_UNRESOLVED:{symbol}")
            continue
        period = periods.pop()
        entry, identity_reason = _identity_for_period(identity_registry, symbol, period)
        effective_from = None if entry is None else entry.effective_from
        errors = {interval: error for interval, _, error in records}
        intraday = tuple(
            _audit_intraday(
                root=monthly_klines_dir,
                symbol=symbol,
                interval=interval,
                period=period,
                effective_from=effective_from,
                failure_error=errors.get(interval),
            )
            for interval in _INTRADAY_INTERVALS
        )
        daily = _audit_daily(
            root=monthly_klines_dir,
            symbol=symbol,
            period=period,
            effective_from=effective_from,
        )
        symbol_reasons = []
        if identity_reason is not None:
            symbol_reasons.append(identity_reason)
        if any(item.status == "UNRESOLVED" for item in intraday):
            symbol_reasons.append("INTRADAY_BOUNDARY_EVIDENCE_UNRESOLVED")
        if daily.status == "UNRESOLVED":
            symbol_reasons.append("DAILY_BOUNDARY_EVIDENCE_UNRESOLVED")
        canonical_symbol_reasons = tuple(sorted(set(symbol_reasons)))
        audit = SymbolLifecycleBoundaryAudit(
            symbol=symbol,
            settled_symbol=f"{symbol}SETTLED",
            period=period,
            identity_effective_from=effective_from,
            identity_source_ref=None if entry is None else entry.source_ref,
            intraday=intraday,
            daily=daily,
            status=(
                "UNRESOLVED" if canonical_symbol_reasons else "CONFIRMED_RELIST_BOUNDARY"
            ),
            reason_codes=canonical_symbol_reasons,
        )
        symbol_audits.append(audit)
        if audit.status == "UNRESOLVED":
            blockers.append(f"LIFECYCLE_BOUNDARY_UNRESOLVED:{symbol}")
        if daily.status == "MIXED_OLD_LIFECYCLE":
            blockers.append(f"DAILY_PRIMARY_ARCHIVE_MIXES_LIFECYCLES:{symbol}/1d/{period}")

    ordered = tuple(symbol_audits)
    canonical_blockers = tuple(sorted(set(blockers)))
    payload: dict[str, Any] = {
        "schema_version": "lifecycle-boundary-audit/0.1.0",
        "normalization_result_hash": normalization.result_hash,
        "identity_registry_hash": identity_registry_hash,
        "target_count": len(ordered),
        "confirmed_relist_count": sum(
            item.status == "CONFIRMED_RELIST_BOUNDARY" for item in ordered
        ),
        "unresolved_count": sum(item.status == "UNRESOLVED" for item in ordered),
        "daily_mixed_lifecycle_count": sum(
            item.daily.status == "MIXED_OLD_LIFECYCLE" for item in ordered
        ),
        "symbols": [item.model_dump(mode="json") for item in ordered],
        "status": "BLOCKED" if canonical_blockers else "AUDITED",
        "blockers": list(canonical_blockers),
        "normalization_result_mutation_authorized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    return LifecycleBoundaryAuditReport.model_validate(
        {
            **payload,
            "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        }
    )


def write_lifecycle_boundary_audit(
    report: LifecycleBoundaryAuditReport, data_dir: Path
) -> Path:
    """Publish one immutable lifecycle-boundary audit by its content hash."""

    report = LifecycleBoundaryAuditReport.model_validate(report.model_dump(mode="json"))
    destination = (
        data_dir
        / "manifests"
        / "lifecycle_boundary_audit"
        / f"{report.report_hash}.json"
    )
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (
        destination.exists() and destination.read_bytes() != content
    ):
        raise LifecycleBoundaryAuditError("existing lifecycle boundary audit changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
