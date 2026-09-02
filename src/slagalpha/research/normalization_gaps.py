"""Audit real monthly kline gaps against selected Universe dependency windows."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.domain.universe import UniverseSnapshot
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.splits import snapshot_sequence_hash

_IDENTITY = re.compile(
    r"^(?P<symbol>[A-Z0-9]+?)/(?P<interval>15m|1h|4h|1d)/(?P<period>\d{4}-\d{2})$"
)
_INTERVAL = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
FINITE_LOOKBACK_BARS = 185
Interval = Literal["15m", "1h", "4h", "1d"]


class NormalizationGapAuditError(ValueError):
    """Raised when raw gap evidence cannot be safely interpreted."""


class GapSpan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_position: int = Field(ge=1)
    before_open_time: datetime
    after_open_time: datetime
    delta_ms: int = Field(gt=0)


class MonthlyGapEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    symbol: str
    interval: Interval
    period: str
    source_sha256: str
    row_count: int = Field(gt=0)
    first_open_time: datetime
    last_open_time: datetime
    gaps: tuple[GapSpan, ...]


class GapDependency(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence: MonthlyGapEvidence
    selected_day_count: int = Field(ge=0)
    direct_overlap_dates: tuple[date, ...]
    lookback_overlap_dates: tuple[date, ...]
    recursive_history_overlap_dates: tuple[date, ...]


class NormalizationGapAuditReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["normalization-gap-audit/0.1.0"] = (
        "normalization-gap-audit/0.1.0"
    )
    normalization_result_hash: str
    daily_snapshot_hash: str
    snapshot_count: int = Field(gt=0)
    failure_file_count: int = Field(ge=0)
    verified_gap_file_count: int = Field(ge=0)
    direct_overlap_day_count: int = Field(ge=0)
    lookback_overlap_day_count: int = Field(ge=0)
    recursive_history_overlap_day_count: int = Field(ge=0)
    finite_lookback_bars: int = Field(gt=0)
    dependencies: tuple[GapDependency, ...]
    status: Literal["BLOCKED", "NO_SELECTED_INPUT_DEPENDENCY"]
    blockers: tuple[str, ...]
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    semantic_gate_relaxation_authorized: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        for value in (
            self.normalization_result_hash,
            self.daily_snapshot_hash,
            self.report_hash,
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("gap audit references must be lowercase SHA-256")
        if self.failure_file_count < len(self.dependencies):
            raise ValueError("gap audit failure count does not reconcile")
        if self.failure_file_count != len(self.dependencies) and not self.blockers:
            raise ValueError("incomplete failure evidence must block")
        identities = tuple(item.evidence.identity for item in self.dependencies)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("gap evidence must be unique and canonical")
        if self.verified_gap_file_count != sum(
            bool(item.evidence.gaps) for item in self.dependencies
        ):
            raise ValueError("verified gap count does not reconcile")
        if self.direct_overlap_day_count != sum(
            len(item.direct_overlap_dates) for item in self.dependencies
        ):
            raise ValueError("direct overlap count does not reconcile")
        if self.lookback_overlap_day_count != sum(
            len(item.lookback_overlap_dates) for item in self.dependencies
        ):
            raise ValueError("lookback overlap count does not reconcile")
        if self.recursive_history_overlap_day_count != sum(
            len(item.recursive_history_overlap_dates) for item in self.dependencies
        ):
            raise ValueError("recursive history overlap count does not reconcile")
        if not self.blockers and (
            self.direct_overlap_day_count or self.lookback_overlap_day_count
            or self.recursive_history_overlap_day_count
            or self.verified_gap_file_count != self.failure_file_count
        ):
            raise ValueError("unresolved gap dependencies must block")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("gap audit blockers must be unique and canonical")
        if (self.status == "BLOCKED") != bool(self.blockers):
            raise ValueError("gap audit status and blockers disagree")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("gap audit content hash mismatch")
        return self


def _month_bounds(period: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def inspect_monthly_gap_archive(path: Path, identity: str) -> MonthlyGapEvidence:
    """Read one official monthly ZIP and record every non-contiguous open-time delta."""

    match = _IDENTITY.fullmatch(identity)
    if match is None:
        raise NormalizationGapAuditError("invalid normalization failure identity")
    interval = cast(Interval, match.group("interval"))
    try:
        month_start, month_end = _month_bounds(match.group("period"))
    except ValueError as error:
        raise NormalizationGapAuditError("invalid normalization failure period") from error
    expected_delta_ms = int(_INTERVAL[interval].total_seconds() * 1000)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    try:
        with zipfile.ZipFile(path) as archive:
            members = tuple(info for info in archive.infolist() if not info.is_dir())
            if len(members) != 1 or not members[0].filename.endswith(".csv"):
                raise NormalizationGapAuditError("monthly archive must contain exactly one CSV")
            if Path(members[0].filename).name != members[0].filename:
                raise NormalizationGapAuditError("monthly archive contains an unsafe member path")
            with archive.open(members[0]) as raw:
                rows = list(csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")))
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise NormalizationGapAuditError("monthly archive cannot be read") from error
    if rows and rows[0] and rows[0][0] == "open_time":
        rows = rows[1:]
    try:
        open_times_ms = tuple(int(row[0]) for row in rows)
    except (IndexError, ValueError) as error:
        raise NormalizationGapAuditError("monthly archive has an invalid open time") from error
    if not open_times_ms:
        raise NormalizationGapAuditError("monthly archive has no candle rows")
    if any(current <= previous for previous, current in zip(
        open_times_ms, open_times_ms[1:], strict=False
    )):
        raise NormalizationGapAuditError("monthly archive open times must strictly increase")
    if any(
        value < int(month_start.timestamp() * 1000)
        or value >= int(month_end.timestamp() * 1000)
        or value % expected_delta_ms
        for value in open_times_ms
    ):
        raise NormalizationGapAuditError("monthly archive open times are outside grid or month")
    gaps = tuple(
        GapSpan(
            row_position=index,
            before_open_time=datetime.fromtimestamp(previous / 1000, UTC),
            after_open_time=datetime.fromtimestamp(current / 1000, UTC),
            delta_ms=current - previous,
        )
        for index, (previous, current) in enumerate(
            zip(open_times_ms, open_times_ms[1:], strict=False), start=1
        )
        if current - previous != expected_delta_ms
    )
    return MonthlyGapEvidence(
        identity=identity,
        symbol=match.group("symbol"),
        interval=interval,
        period=match.group("period"),
        source_sha256=digest.hexdigest(),
        row_count=len(open_times_ms),
        first_open_time=datetime.fromtimestamp(open_times_ms[0] / 1000, UTC),
        last_open_time=datetime.fromtimestamp(open_times_ms[-1] / 1000, UTC),
        gaps=gaps,
    )


def build_normalization_gap_audit(
    *,
    normalization: NormalizationBatchResult,
    snapshots: tuple[UniverseSnapshot, ...],
    expected_daily_snapshot_hash: str,
    monthly_klines_dir: Path,
) -> NormalizationGapAuditReport:
    """Audit finite windows and conservatively block unresolved recursive ATR history."""

    normalization = NormalizationBatchResult.model_validate(
        normalization.model_dump(mode="json")
    )
    snapshots = tuple(
        UniverseSnapshot.model_validate(item.model_dump(mode="json"))
        for item in snapshots
    )
    if snapshot_sequence_hash(snapshots) != expected_daily_snapshot_hash:
        raise NormalizationGapAuditError("Universe snapshot sequence hash mismatch")
    selected_windows: dict[str, list[tuple[date, datetime, datetime]]] = {}
    for snapshot in snapshots:
        for member in snapshot.members:
            selected_windows.setdefault(member.symbol, []).append((
                snapshot.selected_at.date(), snapshot.effective_from, snapshot.effective_to,
            ))
    dependencies: list[GapDependency] = []
    blockers: list[str] = []
    for failure in normalization.failures:
        match = _IDENTITY.fullmatch(failure.identity)
        if match is None:
            blockers.append(f"UNPARSEABLE_FAILURE_IDENTITY:{failure.identity}")
            continue
        symbol = match.group("symbol")
        interval = match.group("interval")
        period = match.group("period")
        path = monthly_klines_dir / symbol / interval / f"{symbol}-{interval}-{period}.zip"
        try:
            evidence = inspect_monthly_gap_archive(path, failure.identity)
        except (OSError, NormalizationGapAuditError):
            blockers.append(f"RAW_GAP_EVIDENCE_UNAVAILABLE:{failure.identity}")
            continue
        if not evidence.gaps or not failure.error.startswith("CandleGapError:"):
            blockers.append(f"FAILURE_NOT_EXPLAINED_BY_RAW_GAP:{failure.identity}")
        month_start, month_end = _month_bounds(period)
        windows = selected_windows.get(symbol, [])
        days = {day for day, _, _ in windows}
        direct = tuple(sorted({
            day for day, start, end in windows if end > month_start and start < month_end
        }))
        lookback = tuple(sorted({
            day for day, start, end in windows if end > month_start
            and start - (_INTERVAL[interval] * FINITE_LOOKBACK_BARS) < month_end
        }))
        recursive = tuple(sorted({day for day, _, end in windows if end > month_start}))
        if lookback:
            blockers.append(f"SELECTED_LOOKBACK_DEPENDS_ON_FAILED_FILE:{failure.identity}")
        if recursive:
            blockers.append(f"RECURSIVE_HISTORY_DEPENDENCY_UNRESOLVED:{failure.identity}")
        dependencies.append(GapDependency(
            evidence=evidence,
            selected_day_count=len(days),
            direct_overlap_dates=direct,
            lookback_overlap_dates=lookback,
            recursive_history_overlap_dates=recursive,
        ))
    ordered = tuple(sorted(dependencies, key=lambda item: item.evidence.identity))
    if len(ordered) != normalization.failed_count:
        blockers.append("FAILED_FILE_EVIDENCE_COUNT_MISMATCH")
    payload: dict[str, Any] = {
        "schema_version": "normalization-gap-audit/0.1.0",
        "normalization_result_hash": normalization.result_hash,
        "daily_snapshot_hash": expected_daily_snapshot_hash,
        "snapshot_count": len(snapshots),
        "failure_file_count": normalization.failed_count,
        "verified_gap_file_count": sum(bool(item.evidence.gaps) for item in ordered),
        "direct_overlap_day_count": sum(len(item.direct_overlap_dates) for item in ordered),
        "lookback_overlap_day_count": sum(len(item.lookback_overlap_dates) for item in ordered),
        "recursive_history_overlap_day_count": sum(
            len(item.recursive_history_overlap_dates) for item in ordered
        ),
        "finite_lookback_bars": FINITE_LOOKBACK_BARS,
        "dependencies": [item.model_dump(mode="json") for item in ordered],
        "status": "BLOCKED" if blockers else "NO_SELECTED_INPUT_DEPENDENCY",
        "blockers": sorted(set(blockers)),
        "strategy_executed": False,
        "locked_test_consumed": False,
        "semantic_gate_relaxation_authorized": False,
    }
    return NormalizationGapAuditReport.model_validate({
        **payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    })


def write_normalization_gap_audit(
    report: NormalizationGapAuditReport, data_dir: Path,
) -> Path:
    report = NormalizationGapAuditReport.model_validate(report.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "normalization_gap_audit" / f"{report.report_hash}.json"
    )
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise NormalizationGapAuditError("existing normalization gap audit changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
