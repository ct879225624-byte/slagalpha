"""Synthetic positive and negative tests for relist lifecycle-boundary auditing."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.data.normalization_batch import NormalizationBatchResult, NormalizationFailure
from slagalpha.domain.universe import (
    ContractIdentityLifecycleEntry,
    ContractIdentityRegistry,
    EvidenceConfidence,
    RegistryVerification,
)
from slagalpha.research.lifecycle_boundaries import (
    LifecycleBoundaryAuditReport,
    build_lifecycle_boundary_audit,
    write_lifecycle_boundary_audit,
)
from test_research_splits import _hash

SYMBOL = "AAAUSDT"
PERIOD = "2025-04"
EFFECTIVE_FROM = datetime(2025, 4, 16, 11, tzinfo=UTC)


def _archive(
    root: Path,
    symbol: str,
    interval: str,
    opens: tuple[datetime, ...],
    *,
    price: str,
) -> None:
    path = root / symbol / interval / f"{symbol}-{interval}-{PERIOD}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["open_time", "open"])
    for value in opens:
        writer.writerow([int(value.timestamp() * 1000), price])
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{symbol}-{interval}-{PERIOD}.csv", buffer.getvalue())


def _write_boundary_archives(root: Path, *, bad_fifteen_minute_boundary: bool = False) -> None:
    day_before = datetime(2025, 4, 15, tzinfo=UTC)
    boundary_day = datetime(2025, 4, 16, tzinfo=UTC)
    primary_opens: dict[str, tuple[datetime, ...]] = {
        "15m": (
            day_before + timedelta(hours=23, minutes=45),
            boundary_day + timedelta(hours=10, minutes=45)
            if bad_fifteen_minute_boundary
            else boundary_day + timedelta(hours=11),
            boundary_day
            + (
                timedelta(hours=11)
                if bad_fifteen_minute_boundary
                else timedelta(hours=11, minutes=15)
            ),
        ),
        "1h": (
            day_before + timedelta(hours=23),
            boundary_day + timedelta(hours=11),
            boundary_day + timedelta(hours=12),
        ),
        "4h": (
            day_before + timedelta(hours=20),
            boundary_day + timedelta(hours=8),
            boundary_day + timedelta(hours=12),
        ),
        "1d": (day_before, boundary_day, boundary_day + timedelta(days=1)),
    }
    settled_opens: dict[str, tuple[datetime, ...]] = {
        "15m": (boundary_day, boundary_day + timedelta(minutes=15)),
        "1h": (boundary_day, boundary_day + timedelta(hours=1)),
        "4h": (boundary_day, boundary_day + timedelta(hours=4)),
        "1d": (boundary_day,),
    }
    for interval, opens in primary_opens.items():
        _archive(root, SYMBOL, interval, opens, price="1")
    for interval, opens in settled_opens.items():
        _archive(root, f"{SYMBOL}SETTLED", interval, opens, price="2")


def _normalization() -> NormalizationBatchResult:
    failures = tuple(
        NormalizationFailure(
            identity=f"{SYMBOL}/{interval}/{PERIOD}",
            error="CandleGapError: synthetic relist gap",
        )
        for interval in ("15m", "1h", "4h")
    )
    return NormalizationBatchResult(
        plan_content_hash=_hash("plan"),
        requested_count=3,
        normalized_count=0,
        reused_count=0,
        failed_count=3,
        normalized_row_count=0,
        normalized_parquet_bytes=0,
        dataset_content_hash=_hash("dataset"),
        failures=failures,
        complete=False,
        result_hash=_hash("normalization"),
    )


def _registry(*, include_symbol: bool = True) -> ContractIdentityRegistry:
    entries: tuple[ContractIdentityLifecycleEntry, ...] = ()
    if include_symbol:
        entries = (
            ContractIdentityLifecycleEntry(
                symbol=SYMBOL,
                base_asset="AAA",
                quote_asset="USDT",
                margin_asset="USDT",
                contract_type="PERPETUAL",
                onboard_date=EFFECTIVE_FROM,
                derived_first_candle_at=EFFECTIVE_FROM,
                effective_from=EFFECTIVE_FROM,
                effective_to=None,
                source_ref="synthetic-identity",
                source_snapshot_hash=_hash("source"),
                reviewed_by="TEST",
                verification_status=RegistryVerification.VERIFIED,
                confidence=EvidenceConfidence.HIGH,
            ),
        )
    return ContractIdentityRegistry(
        registry_version=f"identity-registry-{_hash('identity-registry')}",
        entries=entries,
    )


def _audit(root: Path, *, include_identity: bool = True) -> LifecycleBoundaryAuditReport:
    return build_lifecycle_boundary_audit(
        normalization=_normalization(),
        identity_registry=_registry(include_symbol=include_identity),
        target_symbols=(SYMBOL,),
        monthly_klines_dir=root,
    )


def test_confirmed_relist_and_daily_mixing_are_detected_without_authorization(
    tmp_path: Path,
) -> None:
    _write_boundary_archives(tmp_path)

    report = _audit(tmp_path)

    assert report.confirmed_relist_count == 1
    assert report.unresolved_count == 0
    assert report.daily_mixed_lifecycle_count == 1
    assert report.symbols[0].status == "CONFIRMED_RELIST_BOUNDARY"
    assert all(item.status == "CONFIRMED_BOUNDARY" for item in report.symbols[0].intraday)
    assert report.symbols[0].daily.status == "MIXED_OLD_LIFECYCLE"
    assert report.status == "BLOCKED"
    assert report.blockers == (
        f"DAILY_PRIMARY_ARCHIVE_MIXES_LIFECYCLES:{SYMBOL}/1d/{PERIOD}",
    )
    assert report.normalization_result_mutation_authorized is False
    assert report.atr_reset_authorized is False
    assert report.history_seed_authorized is False
    assert report.historical_rule_gate_relaxation_authorized is False
    assert report.research_authorized is False
    assert report.strategy_executed is False
    assert report.locked_test_consumed is False

    path = write_lifecycle_boundary_audit(report, tmp_path / "data")
    assert write_lifecycle_boundary_audit(report, tmp_path / "data") == path
    assert LifecycleBoundaryAuditReport.model_validate_json(path.read_bytes()) == report
    payload = report.model_dump(mode="json")
    payload["normalization_result_hash"] = "a" * 64
    with pytest.raises(ValidationError, match="content hash"):
        LifecycleBoundaryAuditReport.model_validate(payload)


def test_missing_identity_keeps_aergo_shaped_evidence_unresolved(tmp_path: Path) -> None:
    _write_boundary_archives(tmp_path)

    report = _audit(tmp_path, include_identity=False)

    assert report.confirmed_relist_count == 0
    assert report.unresolved_count == 1
    assert report.symbols[0].status == "UNRESOLVED"
    assert report.symbols[0].identity_effective_from is None
    assert report.symbols[0].reason_codes == (
        "DAILY_BOUNDARY_EVIDENCE_UNRESOLVED",
        "INTRADAY_BOUNDARY_EVIDENCE_UNRESOLVED",
        "VERIFIED_IDENTITY_EFFECTIVE_FROM_UNAVAILABLE",
    )
    assert report.blockers == (f"LIFECYCLE_BOUNDARY_UNRESOLVED:{SYMBOL}",)
    assert report.research_authorized is False


def test_gap_right_edge_that_disagrees_with_identity_fails_closed(tmp_path: Path) -> None:
    _write_boundary_archives(tmp_path, bad_fifteen_minute_boundary=True)

    report = _audit(tmp_path)

    symbol = report.symbols[0]
    assert symbol.status == "UNRESOLVED"
    fifteen_minute = symbol.intraday[0]
    assert fifteen_minute.status == "UNRESOLVED"
    assert "PRIMARY_GAP_AFTER_IDENTITY_BOUNDARY_MISMATCH" in fifteen_minute.reason_codes
    assert report.atr_reset_authorized is False
    assert report.history_seed_authorized is False
