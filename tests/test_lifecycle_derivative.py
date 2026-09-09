"""Synthetic-only acceptance tests for lifecycle-scoped derivatives."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from slagalpha.data.klines import RAW_COLUMNS
from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.lifecycle_boundaries import build_lifecycle_boundary_audit
from slagalpha.research.lifecycle_derivative import (
    LifecycleDerivativeAcceptanceError,
    LifecycleScopedDerivativeAcceptance,
    execute_synthetic_archive_lifecycle_derivative,
    execute_synthetic_lifecycle_derivative,
    publish_synthetic_lifecycle_derivative,
    write_lifecycle_scoped_derivative_acceptance,
)
from slagalpha.research.lifecycle_dry_run import build_lifecycle_dry_run_design_report
from slagalpha.research.lifecycle_executor_contract import (
    LifecycleExecutorInterfaceContract,
    build_lifecycle_executor_interface_contract,
)
from slagalpha.research.lifecycle_remediation import (
    LifecycleNormalizationRemediationPlan,
    build_lifecycle_normalization_remediation_plan,
)
from test_lifecycle_boundaries import (
    PERIOD,
    SYMBOL,
    _normalization,
    _registry,
)


def _rehashed_normalization() -> NormalizationBatchResult:
    normalization = _normalization()
    payload = {
        "plan_content_hash": normalization.plan_content_hash,
        "requested_count": normalization.requested_count,
        "normalized_count": normalization.normalized_count,
        "reused_count": normalization.reused_count,
        "failed_count": normalization.failed_count,
        "normalized_row_count": normalization.normalized_row_count,
        "normalized_parquet_bytes": normalization.normalized_parquet_bytes,
        "dataset_content_hash": normalization.dataset_content_hash,
        "failures": [item.model_dump(mode="json") for item in normalization.failures],
    }
    result_hash = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return normalization.model_copy(update={"result_hash": result_hash})


def _raw_row(open_time: datetime, interval_ms: int) -> list[str]:
    open_ms = int(open_time.timestamp() * 1000)
    return [
        str(open_ms), "100", "110", "90", "105", "10", str(open_ms + interval_ms - 1),
        "1000", "5", "6", "600", "0",
    ]


def _frame(opens: tuple[datetime, ...], interval_ms: int) -> pd.DataFrame:
    return pd.DataFrame(
        [_raw_row(value, interval_ms) for value in opens],
        columns=RAW_COLUMNS,
        dtype=str,
    )


def _archive_bytes(
    root: Path,
    symbol: str,
    interval: str,
    opens: tuple[datetime, ...],
    *,
    price: str,
) -> tuple[str, str, pd.DataFrame]:
    interval_ms = {
        "15m": 900_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }[interval]
    frame = _frame(opens, interval_ms)
    frame.loc[:, ["open", "high", "low", "close"]] = [
        price,
        str(int(price) + 10),
        str(int(price) - 10),
        str(int(price) + 5),
    ]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerows(frame.values.tolist())
    path = root / symbol / interval / f"{symbol}-{interval}-{PERIOD}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{symbol}-{interval}-{PERIOD}.csv", buffer.getvalue())
    archive_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    rows_hash = hashlib.sha256(
        canonical_json_bytes({"rows": frame.astype(str).values.tolist()})
    ).hexdigest()
    return archive_hash, rows_hash, frame


def _plan_and_source(
    tmp_path: Path, *, daily_only_boundary: bool = False
) -> tuple[
    LifecycleNormalizationRemediationPlan,
    dict[str, tuple[str, str, pd.DataFrame]],
]:
    primary_opens = {
        "15m": (
            datetime(2025, 4, 15, 23, 45, tzinfo=UTC),
            datetime(2025, 4, 16, 11, tzinfo=UTC),
            datetime(2025, 4, 16, 11, 15, tzinfo=UTC),
        ),
        "1h": (
            datetime(2025, 4, 15, 23, tzinfo=UTC),
            datetime(2025, 4, 16, 11, tzinfo=UTC),
            datetime(2025, 4, 16, 12, tzinfo=UTC),
        ),
        "4h": (
            datetime(2025, 4, 15, 20, tzinfo=UTC),
            datetime(2025, 4, 16, 8, tzinfo=UTC),
            datetime(2025, 4, 16, 12, tzinfo=UTC),
        ),
        "1d": (
            (datetime(2025, 4, 15, tzinfo=UTC), datetime(2025, 4, 16, tzinfo=UTC))
            if daily_only_boundary
            else (
                datetime(2025, 4, 15, tzinfo=UTC),
                datetime(2025, 4, 16, tzinfo=UTC),
                datetime(2025, 4, 17, tzinfo=UTC),
            )
        ),
    }
    settled_opens = {
        "15m": (datetime(2025, 4, 16, tzinfo=UTC), datetime(2025, 4, 16, 0, 15, tzinfo=UTC)),
        "1h": (datetime(2025, 4, 16, tzinfo=UTC), datetime(2025, 4, 16, 1, tzinfo=UTC)),
        "4h": (datetime(2025, 4, 16, tzinfo=UTC), datetime(2025, 4, 16, 4, tzinfo=UTC)),
        "1d": (datetime(2025, 4, 16, tzinfo=UTC),),
    }
    root = tmp_path / "klines"
    primary_hashes = {
        interval: _archive_bytes(root, SYMBOL, interval, opens, price="100")
        for interval, opens in primary_opens.items()
    }
    for interval, opens in settled_opens.items():
        _archive_bytes(root, f"{SYMBOL}SETTLED", interval, opens, price="200")

    normalization = _rehashed_normalization()
    audit = build_lifecycle_boundary_audit(
        normalization=normalization,
        identity_registry=_registry(),
        target_symbols=(SYMBOL,),
        monthly_klines_dir=root,
    )
    plan = build_lifecycle_normalization_remediation_plan(
        normalization=normalization,
        lifecycle_audit=audit,
        expected_normalization_result_hash=normalization.result_hash,
        expected_lifecycle_audit_hash=audit.report_hash,
        required_unresolved_symbols=(),
    )
    return plan, primary_hashes


def _executor_contract(
    plan: LifecycleNormalizationRemediationPlan,
) -> LifecycleExecutorInterfaceContract:
    dry_run = build_lifecycle_dry_run_design_report(
        plan=plan,
        expected_plan_hash=plan.plan_hash,
        expected_normalization_result_hash=plan.source_normalization_result_hash,
        expected_lifecycle_boundary_audit_hash=plan.lifecycle_boundary_audit_hash,
    )
    return build_lifecycle_executor_interface_contract(
        plan=plan,
        dry_run_report=dry_run,
        expected_plan_hash=plan.plan_hash,
        expected_dry_run_report_hash=dry_run.report_hash,
    )


def _archive_content(tmp_path: Path, symbol: str, interval: str) -> bytes:
    return (
        tmp_path
        / "klines"
        / symbol
        / interval
        / f"{symbol}-{interval}-{PERIOD}.zip"
    ).read_bytes()


def test_synthetic_executor_accepts_filtered_derivative_and_writes_receipt(tmp_path: Path) -> None:
    plan, primary_hashes = _plan_and_source(tmp_path)
    action = next(item for item in plan.actions if item.interval == "4h")
    frame = primary_hashes["4h"][2]
    normalized, acceptance = execute_synthetic_lifecycle_derivative(
        plan,
        action,
        frame,
        source_archive_sha256=primary_hashes["4h"][0],
        evaluated_at=datetime(2025, 5, 1, tzinfo=UTC),
    )
    assert normalized is not None
    assert len(normalized) == 1
    assert acceptance.status == "ACCEPTED"
    assert acceptance.excluded_row_count == 2
    assert acceptance.retained_row_count == 1
    assert acceptance.output_materialized is False
    assert acceptance.research_authorized is False
    path = write_lifecycle_scoped_derivative_acceptance(acceptance, tmp_path / "data")
    assert write_lifecycle_scoped_derivative_acceptance(acceptance, tmp_path / "data") == path
    assert LifecycleScopedDerivativeAcceptance.model_validate_json(path.read_bytes()) == acceptance


def test_synthetic_executor_excludes_empty_boundary_partition(tmp_path: Path) -> None:
    plan, primary_hashes = _plan_and_source(tmp_path, daily_only_boundary=True)
    action = next(item for item in plan.actions if item.interval == "1d")
    frame = primary_hashes["1d"][2]
    normalized, acceptance = execute_synthetic_lifecycle_derivative(
        plan, action, frame, source_archive_sha256=primary_hashes["1d"][0]
    )
    assert normalized is None
    assert acceptance.status == "EXCLUDED_EMPTY_BOUNDARY_PARTITION"
    assert acceptance.retained_row_count == 0
    assert acceptance.derivative_content_hash is None


def test_synthetic_executor_rejects_forged_source_and_plan_membership(tmp_path: Path) -> None:
    plan, primary_hashes = _plan_and_source(tmp_path)
    action = next(item for item in plan.actions if item.interval == "15m")
    frame = primary_hashes["15m"][2].copy()
    frame.loc[1, "close"] = "106"
    with pytest.raises(LifecycleDerivativeAcceptanceError, match="rows hash"):
        execute_synthetic_lifecycle_derivative(
            plan,
            action,
            frame,
            source_archive_sha256=primary_hashes["15m"][0],
        )


def test_synthetic_archive_executor_verifies_both_archives_without_materializing(
    tmp_path: Path,
) -> None:
    plan, _ = _plan_and_source(tmp_path)
    contract = _executor_contract(plan)
    action = next(item for item in plan.actions if item.interval == "4h")
    normalized, acceptance = execute_synthetic_archive_lifecycle_derivative(
        contract,
        plan,
        action,
        expected_contract_hash=contract.contract_hash,
        expected_plan_hash=plan.plan_hash,
        primary_archive=_archive_content(tmp_path, SYMBOL, "4h"),
        settled_archive=_archive_content(tmp_path, f"{SYMBOL}SETTLED", "4h"),
        evaluated_at=datetime(2025, 5, 1, tzinfo=UTC),
    )

    assert normalized is not None
    assert normalized["close"].astype(str).tolist() == ["105"]
    assert acceptance.status == "ACCEPTED"
    assert acceptance.output_materialized is False
    assert acceptance.normalization_execution_authorized is False
    assert acceptance.research_authorized is False
    assert not (tmp_path / "data" / "normalized").exists()


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("primary", "primary archive hash"),
        ("settled", "settled archive hash"),
    ],
)
def test_synthetic_archive_executor_rejects_tampered_archives(
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    plan, _ = _plan_and_source(tmp_path)
    contract = _executor_contract(plan)
    action = next(item for item in plan.actions if item.interval == "15m")
    primary = _archive_content(tmp_path, SYMBOL, "15m")
    settled = _archive_content(tmp_path, f"{SYMBOL}SETTLED", "15m")
    if source == "primary":
        primary += b"tampered"
    else:
        settled += b"tampered"

    with pytest.raises(LifecycleDerivativeAcceptanceError, match=message):
        execute_synthetic_archive_lifecycle_derivative(
            contract,
            plan,
            action,
            expected_contract_hash=contract.contract_hash,
            expected_plan_hash=plan.plan_hash,
            primary_archive=primary,
            settled_archive=settled,
        )


def test_synthetic_archive_executor_rejects_untrusted_contract(tmp_path: Path) -> None:
    plan, _ = _plan_and_source(tmp_path)
    contract = _executor_contract(plan)
    action = plan.actions[0]

    with pytest.raises(LifecycleDerivativeAcceptanceError, match="unexpected executor"):
        execute_synthetic_archive_lifecycle_derivative(
            contract,
            plan,
            action,
            expected_contract_hash="0" * 64,
            expected_plan_hash=plan.plan_hash,
            primary_archive=_archive_content(tmp_path, SYMBOL, action.interval),
            settled_archive=_archive_content(
                tmp_path, f"{SYMBOL}SETTLED", action.interval
            ),
        )


def test_synthetic_publication_is_staged_immutable_and_idempotent(tmp_path: Path) -> None:
    plan, _ = _plan_and_source(tmp_path)
    contract = _executor_contract(plan)
    action = next(item for item in plan.actions if item.interval == "4h")
    normalized, acceptance = execute_synthetic_archive_lifecycle_derivative(
        contract,
        plan,
        action,
        expected_contract_hash=contract.contract_hash,
        expected_plan_hash=plan.plan_hash,
        primary_archive=_archive_content(tmp_path, SYMBOL, "4h"),
        settled_archive=_archive_content(tmp_path, f"{SYMBOL}SETTLED", "4h"),
        evaluated_at=datetime(2025, 5, 1, tzinfo=UTC),
    )
    synthetic_root = tmp_path / "synthetic-workspace"
    normalized_at = datetime(2025, 5, 1, tzinfo=UTC)

    path, parquet_hash = publish_synthetic_lifecycle_derivative(
        contract,
        action,
        normalized,
        acceptance,
        expected_contract_hash=contract.contract_hash,
        synthetic_workspace_root=synthetic_root,
        normalized_at=normalized_at,
    )
    repeated_path, repeated_hash = publish_synthetic_lifecycle_derivative(
        contract,
        action,
        normalized,
        acceptance,
        expected_contract_hash=contract.contract_hash,
        synthetic_workspace_root=synthetic_root,
        normalized_at=normalized_at,
    )

    assert path == repeated_path
    assert parquet_hash == repeated_hash
    assert path.suffix == ".parquet"
    assert path.is_relative_to(synthetic_root.resolve())
    assert not list(synthetic_root.glob(".lifecycle-stage-*"))
    assert not (synthetic_root / contract.frozen_namespace).exists()


def test_synthetic_publication_rejects_existing_conflict(tmp_path: Path) -> None:
    plan, _ = _plan_and_source(tmp_path)
    contract = _executor_contract(plan)
    action = next(item for item in plan.actions if item.interval == "4h")
    normalized, acceptance = execute_synthetic_archive_lifecycle_derivative(
        contract,
        plan,
        action,
        expected_contract_hash=contract.contract_hash,
        expected_plan_hash=plan.plan_hash,
        primary_archive=_archive_content(tmp_path, SYMBOL, "4h"),
        settled_archive=_archive_content(tmp_path, f"{SYMBOL}SETTLED", "4h"),
        evaluated_at=datetime(2025, 5, 1, tzinfo=UTC),
    )
    synthetic_root = tmp_path / "synthetic-workspace"
    path, _ = publish_synthetic_lifecycle_derivative(
        contract,
        action,
        normalized,
        acceptance,
        expected_contract_hash=contract.contract_hash,
        synthetic_workspace_root=synthetic_root,
        normalized_at=datetime(2025, 5, 1, tzinfo=UTC),
    )
    path.write_bytes(b"conflict")

    with pytest.raises(LifecycleDerivativeAcceptanceError, match="conflicts"):
        publish_synthetic_lifecycle_derivative(
            contract,
            action,
            normalized,
            acceptance,
            expected_contract_hash=contract.contract_hash,
            synthetic_workspace_root=synthetic_root,
            normalized_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
    assert not list(synthetic_root.glob(".lifecycle-stage-*"))


def test_synthetic_publication_uses_receipt_only_for_empty_partition(
    tmp_path: Path,
) -> None:
    plan, _ = _plan_and_source(tmp_path, daily_only_boundary=True)
    contract = _executor_contract(plan)
    action = next(item for item in plan.actions if item.interval == "1d")
    normalized, acceptance = execute_synthetic_archive_lifecycle_derivative(
        contract,
        plan,
        action,
        expected_contract_hash=contract.contract_hash,
        expected_plan_hash=plan.plan_hash,
        primary_archive=_archive_content(tmp_path, SYMBOL, "1d"),
        settled_archive=_archive_content(tmp_path, f"{SYMBOL}SETTLED", "1d"),
    )
    synthetic_root = tmp_path / "synthetic-workspace"

    path, _ = publish_synthetic_lifecycle_derivative(
        contract,
        action,
        normalized,
        acceptance,
        expected_contract_hash=contract.contract_hash,
        synthetic_workspace_root=synthetic_root,
        normalized_at=datetime(2025, 5, 1, tzinfo=UTC),
    )

    assert path.suffix == ".json"
    assert not list(synthetic_root.rglob("*.parquet"))


def test_synthetic_publication_rejects_repository_normalized_namespace(
    tmp_path: Path,
) -> None:
    plan, _ = _plan_and_source(tmp_path)
    contract = _executor_contract(plan)
    action = next(item for item in plan.actions if item.interval == "4h")
    normalized, acceptance = execute_synthetic_archive_lifecycle_derivative(
        contract,
        plan,
        action,
        expected_contract_hash=contract.contract_hash,
        expected_plan_hash=plan.plan_hash,
        primary_archive=_archive_content(tmp_path, SYMBOL, "4h"),
        settled_archive=_archive_content(tmp_path, f"{SYMBOL}SETTLED", "4h"),
        evaluated_at=datetime(2025, 5, 1, tzinfo=UTC),
    )
    repository_root = Path(__file__).resolve().parents[1]

    with pytest.raises(LifecycleDerivativeAcceptanceError, match="repository normalized"):
        publish_synthetic_lifecycle_derivative(
            contract,
            action,
            normalized,
            acceptance,
            expected_contract_hash=contract.contract_hash,
            synthetic_workspace_root=repository_root,
            normalized_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
