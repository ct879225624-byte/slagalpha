"""Explicitly authorized local execution of lifecycle boundary-month derivatives."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import ArchiveSpec, sha256_file
from slagalpha.data.klines import read_archive_csv, write_normalized_parquet
from slagalpha.reporting.run_manifest import (
    RunManifestError,
    _publish_immutable,
    canonical_json_bytes,
)
from slagalpha.research.lifecycle_derivative import (
    LifecycleDerivativeAcceptanceError,
    _source_rows_hash,
    execute_synthetic_lifecycle_derivative,
)
from slagalpha.research.lifecycle_executor_contract import (
    LifecycleExecutorInterfaceContract,
)
from slagalpha.research.lifecycle_remediation import (
    LifecycleIntervalRemediationAction,
    LifecycleNormalizationRemediationPlan,
)


class LifecycleRealExecutionError(RuntimeError):
    """Raised when authorized real execution cannot preserve frozen boundaries."""


class LifecycleRealExecutionAuthorization(BaseModel):
    """Content-addressed record of the user's narrowly scoped execution approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-real-execution-authorization/0.1.0"] = (
        "lifecycle-real-execution-authorization/0.1.0"
    )
    approval: Literal["USER_APPROVED_REAL_LOCAL_LIFECYCLE_NORMALIZATION"]
    authorized_at: datetime
    executor_contract_hash: str
    remediation_plan_hash: str
    action_count: int = Field(gt=0)
    expected_source_row_count: int = Field(ge=0)
    expected_excluded_row_count: int = Field(ge=0)
    expected_retained_row_count: int = Field(ge=0)
    read_local_archives_authorized: Literal[True] = True
    lifecycle_derivative_materialization_authorized: Literal[True] = True
    market_data_download_authorized: Literal[False] = False
    frozen_normalization_mutation_authorized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    authorization_hash: str

    @field_validator("executor_contract_hash", "remediation_plan_hash", "authorization_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("real execution references must be lowercase SHA-256")
        return value

    @field_validator("authorized_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("authorization time must use UTC")
        if value.microsecond % 1000:
            raise ValueError("authorization time must use millisecond precision")
        return value

    @model_validator(mode="after")
    def validate_authorization(self) -> Self:
        if self.expected_excluded_row_count + self.expected_retained_row_count != (
            self.expected_source_row_count
        ):
            raise ValueError("authorized row counts do not reconcile")
        payload = self.model_dump(mode="json", exclude={"authorization_hash"})
        if self.authorization_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("real execution authorization hash mismatch")
        return self


class LifecycleRealActionResult(BaseModel):
    """One verified real boundary-month derivative or exclusion receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    interval: Literal["15m", "1h", "4h", "1d"]
    evidence_period: str
    action_hash: str
    primary_archive_sha256: str
    primary_rows_sha256: str
    settled_archive_sha256: str
    settled_rows_sha256: str
    source_row_count: int = Field(gt=0)
    excluded_row_count: int = Field(ge=0)
    retained_row_count: int = Field(ge=0)
    derivative_content_hash: str | None
    output_relative_path: str
    output_sha256: str
    status: Literal["MATERIALIZED", "EXCLUDED_EMPTY_BOUNDARY_PARTITION"]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.excluded_row_count + self.retained_row_count != self.source_row_count:
            raise ValueError("real action rows do not reconcile")
        if (self.derivative_content_hash is None) != (
            self.status == "EXCLUDED_EMPTY_BOUNDARY_PARTITION"
        ):
            raise ValueError("real action status and derivative hash disagree")
        return self


class LifecycleRealExecutionReceipt(BaseModel):
    """Content-addressed completion marker for the authorized real batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-real-execution/0.1.0"] = "lifecycle-real-execution/0.1.0"
    authorization_hash: str
    executor_contract_hash: str
    remediation_plan_hash: str
    source_normalization_result_hash: str
    action_count: int = Field(gt=0)
    materialized_action_count: int = Field(ge=0)
    excluded_action_count: int = Field(ge=0)
    source_row_count: int = Field(ge=0)
    excluded_row_count: int = Field(ge=0)
    retained_row_count: int = Field(ge=0)
    outputs: tuple[LifecycleRealActionResult, ...]
    frozen_normalization_preserved: Literal[True] = True
    real_output_materialized: Literal[True] = True
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    status: Literal["MATERIALIZED_BOUNDARY_MONTHS"] = "MATERIALIZED_BOUNDARY_MONTHS"
    receipt_hash: str

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        keys = tuple((item.symbol, item.interval) for item in self.outputs)
        if keys != tuple(sorted(set(keys))) or len(self.outputs) != self.action_count:
            raise ValueError("real execution outputs must be complete and canonical")
        if self.materialized_action_count != sum(
            item.status == "MATERIALIZED" for item in self.outputs
        ) or self.excluded_action_count != sum(
            item.status == "EXCLUDED_EMPTY_BOUNDARY_PARTITION" for item in self.outputs
        ):
            raise ValueError("real execution action statuses do not reconcile")
        for field in ("source_row_count", "excluded_row_count", "retained_row_count"):
            if getattr(self, field) != sum(getattr(item, field) for item in self.outputs):
                raise ValueError(f"real execution {field} does not reconcile")
        payload = self.model_dump(mode="json", exclude={"receipt_hash"})
        if self.receipt_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("real execution receipt hash mismatch")
        return self


def build_real_execution_authorization(
    contract: LifecycleExecutorInterfaceContract,
    plan: LifecycleNormalizationRemediationPlan,
    *,
    expected_contract_hash: str,
    expected_plan_hash: str,
    authorized_at: datetime,
    approval: Literal["USER_APPROVED_REAL_LOCAL_LIFECYCLE_NORMALIZATION"],
) -> LifecycleRealExecutionAuthorization:
    contract = LifecycleExecutorInterfaceContract.model_validate(contract.model_dump(mode="json"))
    plan = LifecycleNormalizationRemediationPlan.model_validate(plan.model_dump(mode="json"))
    if contract.contract_hash != expected_contract_hash or plan.plan_hash != expected_plan_hash:
        raise LifecycleRealExecutionError("authorization references unexpected trusted inputs")
    if contract.remediation_plan_hash != plan.plan_hash:
        raise LifecycleRealExecutionError("executor contract does not reference the plan")
    payload: dict[str, Any] = {
        "approval": approval,
        "authorized_at": authorized_at,
        "executor_contract_hash": contract.contract_hash,
        "remediation_plan_hash": plan.plan_hash,
        "action_count": plan.action_count,
        "expected_source_row_count": plan.boundary_month_source_row_count,
        "expected_excluded_row_count": plan.boundary_month_excluded_row_count,
        "expected_retained_row_count": plan.boundary_month_expected_retained_row_count,
        "read_local_archives_authorized": True,
        "lifecycle_derivative_materialization_authorized": True,
        "market_data_download_authorized": False,
        "frozen_normalization_mutation_authorized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    candidate = LifecycleRealExecutionAuthorization.model_construct(
        **payload, authorization_hash="0" * 64
    )
    authorization_hash = hashlib.sha256(
        canonical_json_bytes(candidate.model_dump(mode="json", exclude={"authorization_hash"}))
    ).hexdigest()
    return LifecycleRealExecutionAuthorization.model_validate(
        {**payload, "authorization_hash": authorization_hash}
    )


def write_real_execution_authorization(
    authorization: LifecycleRealExecutionAuthorization, data_dir: Path
) -> Path:
    authorization = LifecycleRealExecutionAuthorization.model_validate(
        authorization.model_dump(mode="json")
    )
    destination = (
        data_dir
        / "manifests"
        / "lifecycle_real_authorization"
        / f"{authorization.authorization_hash}.json"
    )
    content = canonical_json_bytes(authorization.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise LifecycleRealExecutionError("existing real authorization changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination


def _publish_output(
    frame: pd.DataFrame,
    destination: Path,
    *,
    normalized_at: datetime,
    content_hash: str,
    source_hash: str,
    staging_root: Path,
) -> str:
    with TemporaryDirectory(prefix="action-", dir=staging_root) as directory:
        staged = Path(directory) / destination.name
        staged_hash = write_normalized_parquet(
            frame,
            staged,
            normalized_at=normalized_at,
            normalized_content_hash=content_hash,
            source_file_hash=source_hash,
        )
        content = staged.read_bytes()
        if hashlib.sha256(content).hexdigest() != staged_hash:
            raise LifecycleRealExecutionError("staged real derivative hash mismatch")
        if destination.is_symlink() or (
            destination.exists() and destination.read_bytes() != content
        ):
            raise LifecycleRealExecutionError(
                "existing real derivative conflicts with staged output"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _publish_immutable(destination, content)
        except RunManifestError as error:
            raise LifecycleRealExecutionError("real derivative publication conflict") from error
    return staged_hash


def execute_real_lifecycle_derivatives(
    authorization: LifecycleRealExecutionAuthorization,
    contract: LifecycleExecutorInterfaceContract,
    plan: LifecycleNormalizationRemediationPlan,
    *,
    raw_klines_dir: Path,
    workspace_root: Path,
    normalized_at: datetime,
) -> tuple[LifecycleRealExecutionReceipt, Path]:
    """Execute only the 32 authorized, content-addressed boundary-month actions."""

    authorization = LifecycleRealExecutionAuthorization.model_validate(
        authorization.model_dump(mode="json")
    )
    contract = LifecycleExecutorInterfaceContract.model_validate(contract.model_dump(mode="json"))
    plan = LifecycleNormalizationRemediationPlan.model_validate(plan.model_dump(mode="json"))
    if authorization.executor_contract_hash != contract.contract_hash or (
        authorization.remediation_plan_hash != plan.plan_hash
    ):
        raise LifecycleRealExecutionError("authorization lineage does not match execution inputs")
    counts = (
        plan.action_count,
        plan.boundary_month_source_row_count,
        plan.boundary_month_excluded_row_count,
        plan.boundary_month_expected_retained_row_count,
    )
    if counts != (
        authorization.action_count,
        authorization.expected_source_row_count,
        authorization.expected_excluded_row_count,
        authorization.expected_retained_row_count,
    ):
        raise LifecycleRealExecutionError("authorization counts do not match the plan")
    authorization_path = (
        workspace_root
        / "data"
        / "manifests"
        / "lifecycle_real_authorization"
        / f"{authorization.authorization_hash}.json"
    )
    expected_authorization = canonical_json_bytes(authorization.model_dump(mode="json"))
    if (
        not authorization_path.is_file()
        or authorization_path.read_bytes() != expected_authorization
    ):
        raise LifecycleRealExecutionError("durable real execution authorization is missing")

    output_root = (workspace_root / contract.derivative_namespace).resolve()
    frozen_root = (workspace_root / contract.frozen_namespace).resolve()
    if output_root == frozen_root or output_root.is_relative_to(frozen_root):
        raise LifecycleRealExecutionError(
            "real derivative output collides with frozen normalization"
        )
    staging_root = output_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    results: list[LifecycleRealActionResult] = []
    try:
        for action in sorted(plan.actions, key=lambda item: (item.symbol, item.interval)):
            action = LifecycleIntervalRemediationAction.model_validate(
                action.model_dump(mode="json")
            )
            primary = (
                raw_klines_dir
                / action.symbol
                / action.interval
                / f"{action.symbol}-{action.interval}-{action.evidence_period}.zip"
            )
            settled = (
                raw_klines_dir
                / action.settled_symbol
                / action.interval
                / f"{action.settled_symbol}-{action.interval}-{action.evidence_period}.zip"
            )
            if not primary.is_file() or not settled.is_file():
                raise LifecycleRealExecutionError("authorized source archive is missing")
            if (
                sha256_file(primary) != action.primary_archive_sha256
                or sha256_file(settled) != action.settled_archive_sha256
            ):
                raise LifecycleRealExecutionError("authorized source archive hash changed")
            year, month = (int(part) for part in action.evidence_period.split("-"))
            primary_frame = read_archive_csv(
                primary,
                ArchiveSpec(symbol=action.symbol, interval=action.interval, year=year, month=month),
            )
            settled_frame = read_archive_csv(
                settled,
                ArchiveSpec(
                    symbol=action.settled_symbol, interval=action.interval, year=year, month=month
                ),
            )
            if (
                _source_rows_hash(primary_frame) != action.primary_rows_sha256
                or _source_rows_hash(settled_frame) != action.settled_rows_sha256
            ):
                raise LifecycleRealExecutionError("authorized source rows changed")
            if len(settled_frame) != action.settled_evidence_row_count:
                raise LifecycleRealExecutionError("SETTLED evidence row count changed")
            try:
                normalized, acceptance = execute_synthetic_lifecycle_derivative(
                    plan,
                    action,
                    primary_frame,
                    source_archive_sha256=action.primary_archive_sha256,
                    evaluated_at=normalized_at,
                )
            except LifecycleDerivativeAcceptanceError as error:
                raise LifecycleRealExecutionError("real lifecycle computation failed") from error
            action_hash = hashlib.sha256(
                canonical_json_bytes(action.model_dump(mode="json"))
            ).hexdigest()
            relative = contract.output_partition_template.format(
                interval=action.interval,
                symbol=action.symbol,
                year=year,
                month=f"{month:02d}",
                action_hash=action_hash[:16],
            )
            destination = (output_root / relative).resolve()
            if not destination.is_relative_to(output_root):
                raise LifecycleRealExecutionError("real derivative path escapes output namespace")
            if normalized is None:
                destination = destination.with_suffix(".exclusion.json")
                content = canonical_json_bytes(
                    {
                        "schema_version": "lifecycle-real-exclusion/0.1.0",
                        "authorization_hash": authorization.authorization_hash,
                        "action_hash": action_hash,
                        "symbol": action.symbol,
                        "interval": action.interval,
                        "evidence_period": action.evidence_period,
                        "status": "EXCLUDED_EMPTY_BOUNDARY_PARTITION",
                    }
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_symlink() or (
                    destination.exists() and destination.read_bytes() != content
                ):
                    raise LifecycleRealExecutionError("existing real exclusion changed")
                _publish_immutable(destination, content)
                output_hash = hashlib.sha256(content).hexdigest()
                status: Literal["MATERIALIZED", "EXCLUDED_EMPTY_BOUNDARY_PARTITION"] = (
                    "EXCLUDED_EMPTY_BOUNDARY_PARTITION"
                )
            else:
                if acceptance.derivative_content_hash is None:
                    raise LifecycleRealExecutionError("real derivative content hash is missing")
                output_hash = _publish_output(
                    normalized,
                    destination,
                    normalized_at=normalized_at,
                    content_hash=acceptance.derivative_content_hash,
                    source_hash=action.primary_archive_sha256,
                    staging_root=staging_root,
                )
                status = "MATERIALIZED"
            results.append(
                LifecycleRealActionResult(
                    symbol=action.symbol,
                    interval=action.interval,
                    evidence_period=action.evidence_period,
                    action_hash=action_hash,
                    primary_archive_sha256=action.primary_archive_sha256,
                    primary_rows_sha256=action.primary_rows_sha256,
                    settled_archive_sha256=action.settled_archive_sha256,
                    settled_rows_sha256=action.settled_rows_sha256,
                    source_row_count=acceptance.source_row_count,
                    excluded_row_count=acceptance.excluded_row_count,
                    retained_row_count=acceptance.retained_row_count,
                    derivative_content_hash=acceptance.derivative_content_hash,
                    output_relative_path=destination.relative_to(workspace_root).as_posix(),
                    output_sha256=output_hash,
                    status=status,
                )
            )
    finally:
        if staging_root.exists() and not any(staging_root.iterdir()):
            staging_root.rmdir()

    output_tuple = tuple(results)
    payload: dict[str, Any] = {
        "authorization_hash": authorization.authorization_hash,
        "executor_contract_hash": contract.contract_hash,
        "remediation_plan_hash": plan.plan_hash,
        "source_normalization_result_hash": plan.source_normalization_result_hash,
        "action_count": len(output_tuple),
        "materialized_action_count": sum(item.status == "MATERIALIZED" for item in output_tuple),
        "excluded_action_count": sum(
            item.status == "EXCLUDED_EMPTY_BOUNDARY_PARTITION" for item in output_tuple
        ),
        "source_row_count": sum(item.source_row_count for item in output_tuple),
        "excluded_row_count": sum(item.excluded_row_count for item in output_tuple),
        "retained_row_count": sum(item.retained_row_count for item in output_tuple),
        "outputs": output_tuple,
        "frozen_normalization_preserved": True,
        "real_output_materialized": True,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "status": "MATERIALIZED_BOUNDARY_MONTHS",
    }
    candidate = LifecycleRealExecutionReceipt.model_construct(**payload, receipt_hash="0" * 64)
    receipt_hash = hashlib.sha256(
        canonical_json_bytes(candidate.model_dump(mode="json", exclude={"receipt_hash"}))
    ).hexdigest()
    receipt = LifecycleRealExecutionReceipt.model_validate(
        {**payload, "receipt_hash": receipt_hash}
    )
    receipt_path = (
        workspace_root
        / "data"
        / "manifests"
        / "lifecycle_real_execution"
        / f"{receipt.receipt_hash}.json"
    )
    content = canonical_json_bytes(receipt.model_dump(mode="json"))
    if receipt_path.is_symlink() or (
        receipt_path.exists() and receipt_path.read_bytes() != content
    ):
        raise LifecycleRealExecutionError("existing real execution receipt changed")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(receipt_path, content)
    return receipt, receipt_path
