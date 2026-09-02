"""Synthetic ZIP evidence tests for normalization-gap auditing."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.data.normalization_batch import NormalizationBatchResult, NormalizationFailure
from slagalpha.research.normalization_gaps import (
    NormalizationGapAuditError,
    NormalizationGapAuditReport,
    build_normalization_gap_audit,
    inspect_monthly_gap_archive,
    write_normalization_gap_audit,
)
from slagalpha.research.splits import snapshot_sequence_hash
from test_research_splits import _hash, _snapshot


def _archive(path: Path, opens: tuple[datetime, ...], *, member: str = "XUSDT-1h.csv") -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["open_time", "open"])
    for value in opens:
        writer.writerow([int(value.timestamp() * 1000), "1"])
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, buffer.getvalue())


def test_real_gap_is_recorded_without_filling_missing_candles(tmp_path: Path) -> None:
    start = datetime(2025, 4, 1, tzinfo=UTC)
    path = tmp_path / "XUSDT-1h-2025-04.zip"
    _archive(path, (start, start + timedelta(hours=1), start + timedelta(hours=3)))
    evidence = inspect_monthly_gap_archive(path, "XUSDT/1h/2025-04")
    assert evidence.row_count == 3
    assert len(evidence.gaps) == 1
    assert evidence.gaps[0].delta_ms == 2 * 60 * 60 * 1000
    assert evidence.gaps[0].row_position == 2


def test_contiguous_archive_has_no_gap_and_unsafe_member_is_rejected(tmp_path: Path) -> None:
    start = datetime(2025, 4, 1, tzinfo=UTC)
    path = tmp_path / "XUSDT-1h-2025-04.zip"
    _archive(path, (start, start + timedelta(hours=1)))
    assert inspect_monthly_gap_archive(path, "XUSDT/1h/2025-04").gaps == ()
    unsafe = tmp_path / "unsafe.zip"
    _archive(unsafe, (start,), member="../XUSDT-1h.csv")
    with pytest.raises(NormalizationGapAuditError, match="unsafe"):
        inspect_monthly_gap_archive(unsafe, "XUSDT/1h/2025-04")


@pytest.mark.parametrize("identity", ["X/5m/2025-04", "../X/1h/2025-04", "X/1h/april"])
def test_invalid_failure_identity_is_rejected(tmp_path: Path, identity: str) -> None:
    with pytest.raises(NormalizationGapAuditError, match="identity"):
        inspect_monthly_gap_archive(tmp_path / "missing.zip", identity)


def _audit(
    tmp_path: Path, day: date, *, missing: bool = False,
) -> NormalizationGapAuditReport:
    normalization = NormalizationBatchResult(
        plan_content_hash=_hash("plan"), requested_count=1, normalized_count=0,
        reused_count=0, failed_count=1, normalized_row_count=0, normalized_parquet_bytes=0,
        dataset_content_hash=_hash("dataset"),
        failures=(NormalizationFailure(
            identity="AAAUSDT/1h/2025-04", error="CandleGapError: synthetic gap",
        ),), complete=False, result_hash=_hash("normalization"),
    )
    if not missing:
        path = tmp_path / "AAAUSDT" / "1h" / "AAAUSDT-1h-2025-04.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        start = datetime(2025, 4, 1, tzinfo=UTC)
        _archive(path, (start, start + timedelta(hours=2)))
    snapshots = (_snapshot(day),)
    return build_normalization_gap_audit(
        normalization=normalization, snapshots=snapshots,
        expected_daily_snapshot_hash=snapshot_sequence_hash(snapshots),
        monthly_klines_dir=tmp_path,
    )


def test_later_gap_has_no_dependency_but_never_authorizes_a_replay(tmp_path: Path) -> None:
    report = _audit(tmp_path, date(2025, 3, 1))
    assert report.status == "NO_SELECTED_INPUT_DEPENDENCY"
    assert report.recursive_history_overlap_day_count == 0
    assert report.semantic_gate_relaxation_authorized is False
    path = write_normalization_gap_audit(report, tmp_path / "data")
    assert write_normalization_gap_audit(report, tmp_path / "data") == path
    assert NormalizationGapAuditReport.model_validate_json(path.read_bytes()) == report
    payload = report.model_dump(mode="json")
    payload["snapshot_count"] += 1
    with pytest.raises(ValidationError, match="content hash"):
        NormalizationGapAuditReport.model_validate(payload)


def test_distant_past_gap_still_blocks_recursive_atr_dependency(tmp_path: Path) -> None:
    report = _audit(tmp_path, date(2025, 6, 1))
    assert report.direct_overlap_day_count == report.lookback_overlap_day_count == 0
    assert report.recursive_history_overlap_day_count == 1
    assert report.status == "BLOCKED"
    assert report.blockers == (
        "RECURSIVE_HISTORY_DEPENDENCY_UNRESOLVED:AAAUSDT/1h/2025-04",
    )


def test_effective_window_crossing_month_boundary_counts_overlap(tmp_path: Path) -> None:
    report = _audit(tmp_path, date(2025, 3, 31))
    assert report.direct_overlap_day_count == report.lookback_overlap_day_count == 1
    assert report.recursive_history_overlap_day_count == 1
    assert report.status == "BLOCKED"


def test_missing_raw_archive_produces_blocked_report_not_exception(tmp_path: Path) -> None:
    report = _audit(tmp_path, date(2025, 3, 1), missing=True)
    assert report.status == "BLOCKED"
    assert report.failure_file_count == 1
    assert report.dependencies == ()
    assert "FAILED_FILE_EVIDENCE_COUNT_MISMATCH" in report.blockers


@pytest.mark.parametrize("offsets", [(0, 0), (1, 0), (0, -1), (0, 31 * 24)])
def test_invalid_open_times_are_rejected(tmp_path: Path, offsets: tuple[int, int]) -> None:
    path = tmp_path / "invalid.zip"
    start = datetime(2025, 4, 1, tzinfo=UTC)
    _archive(path, tuple(start + timedelta(hours=offset) for offset in offsets))
    with pytest.raises(NormalizationGapAuditError):
        inspect_monthly_gap_archive(path, "XUSDT/1h/2025-04")
