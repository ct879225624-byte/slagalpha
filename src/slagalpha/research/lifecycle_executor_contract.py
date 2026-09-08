"""Freeze the safety interface for a future lifecycle derivative executor."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.lifecycle_dry_run import LifecycleDryRunDesignReport
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_CHECKS = (
    "ACTION_MEMBERSHIP_IN_TRUSTED_PLAN",
    "PRIMARY_ARCHIVE_SHA256",
    "PRIMARY_ROWS_SHA256",
    "SETTLED_ARCHIVE_SHA256_EVIDENCE_ONLY",
    "SETTLED_ROWS_SHA256_EVIDENCE_ONLY",
    "EXACT_RETAIN_FROM_OPEN_TIME",
    "SOURCE_EXCLUDED_RETAINED_ROW_CONSERVATION",
    "NORMALIZED_SCHEMA_AND_CONTENT_HASH",
    "FROZEN_NAMESPACE_NON_MUTATION",
)
_BLOCKERS = (
    "EXECUTOR_IMPLEMENTATION_NOT_AUTHORIZED",
    "REAL_DERIVATIVE_EXECUTION_NOT_AUTHORIZED",
    "HISTORICAL_RULE_GATE_REMAINS_BLOCKED",
    "RESEARCH_AUTHORIZATION_REMAINS_FALSE",
)


class LifecycleExecutorContractError(ValueError):
    """Raised when the frozen executor interface does not fail closed."""


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = {
        "schema_version": "lifecycle-derivative-executor-interface/0.1.0",
        **payload,
    }
    canonical.pop("contract_hash", None)
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


class LifecycleExecutorInterfaceContract(BaseModel):
    """Non-executing contract for future production implementation review."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-derivative-executor-interface/0.1.0"] = (
        "lifecycle-derivative-executor-interface/0.1.0"
    )
    remediation_plan_hash: str
    dry_run_design_report_hash: str
    source_normalization_result_hash: str
    lifecycle_boundary_audit_hash: str
    input_mode: Literal["VERIFIED_LOCAL_PRIMARY_ARCHIVE_ONLY"] = (
        "VERIFIED_LOCAL_PRIMARY_ARCHIVE_ONLY"
    )
    settled_input_mode: Literal["EVIDENCE_ONLY_NEVER_MERGE"] = (
        "EVIDENCE_ONLY_NEVER_MERGE"
    )
    frozen_namespace: Literal["data/normalized/klines"] = "data/normalized/klines"
    derivative_namespace: Literal["data/normalized/lifecycle_scoped/v0.1.0"] = (
        "data/normalized/lifecycle_scoped/v0.1.0"
    )
    output_partition_template: Literal[
        "interval={interval}/symbol={symbol}/year={year}/month={month}/part-{action_hash}.parquet"
    ] = "interval={interval}/symbol={symbol}/year={year}/month={month}/part-{action_hash}.parquet"
    required_checks: tuple[str, ...] = _REQUIRED_CHECKS
    publication_policy: Literal["STAGE_VALIDATE_ATOMIC_RENAME"] = (
        "STAGE_VALIDATE_ATOMIC_RENAME"
    )
    resume_policy: Literal["REUSE_IDENTICAL_CONTENT_ONLY"] = (
        "REUSE_IDENTICAL_CONTENT_ONLY"
    )
    conflict_policy: Literal["FAIL_CLOSED_NEVER_OVERWRITE"] = (
        "FAIL_CLOSED_NEVER_OVERWRITE"
    )
    empty_partition_policy: Literal["EXCLUSION_RECEIPT_ONLY"] = (
        "EXCLUSION_RECEIPT_ONLY"
    )
    action_count: int = Field(gt=0)
    expected_source_row_count: int = Field(ge=0)
    expected_excluded_row_count: int = Field(ge=0)
    expected_retained_row_count: int = Field(ge=0)
    frozen_normalization_preserved: Literal[True] = True
    raw_archive_mutation_authorized: Literal[False] = False
    executor_implementation_authorized: Literal[False] = False
    normalization_execution_authorized: Literal[False] = False
    output_materialized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    status: Literal["BLOCKED"] = "BLOCKED"
    blockers: tuple[str, ...] = _BLOCKERS
    contract_hash: str

    @field_validator(
        "remediation_plan_hash",
        "dry_run_design_report_hash",
        "source_normalization_result_hash",
        "lifecycle_boundary_audit_hash",
        "contract_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("executor contract references must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.required_checks != _REQUIRED_CHECKS:
            raise ValueError("executor safety checks must be complete and ordered")
        if self.blockers != _BLOCKERS:
            raise ValueError("executor contract blockers must remain frozen")
        if self.expected_excluded_row_count + self.expected_retained_row_count != (
            self.expected_source_row_count
        ):
            raise ValueError("executor contract row counts do not reconcile")
        payload = self.model_dump(mode="json", exclude={"contract_hash"})
        if self.contract_hash != _hash_payload(payload):
            raise ValueError("lifecycle executor contract hash mismatch")
        return self


def build_lifecycle_executor_interface_contract(
    *,
    plan: LifecycleNormalizationRemediationPlan,
    dry_run_report: LifecycleDryRunDesignReport,
    expected_plan_hash: str,
    expected_dry_run_report_hash: str,
) -> LifecycleExecutorInterfaceContract:
    """Freeze interface requirements without implementing or invoking the executor."""

    plan = LifecycleNormalizationRemediationPlan.model_validate(
        plan.model_dump(mode="json")
    )
    dry_run_report = LifecycleDryRunDesignReport.model_validate(
        dry_run_report.model_dump(mode="json")
    )
    if plan.plan_hash != expected_plan_hash:
        raise LifecycleExecutorContractError("unexpected remediation plan")
    if dry_run_report.report_hash != expected_dry_run_report_hash:
        raise LifecycleExecutorContractError("unexpected dry-run design report")
    if dry_run_report.remediation_plan_hash != plan.plan_hash:
        raise LifecycleExecutorContractError("dry-run report does not reference the plan")
    if dry_run_report.normalization_result_hash != plan.source_normalization_result_hash:
        raise LifecycleExecutorContractError("normalization lineage does not reconcile")
    if dry_run_report.lifecycle_boundary_audit_hash != plan.lifecycle_boundary_audit_hash:
        raise LifecycleExecutorContractError("lifecycle audit lineage does not reconcile")
    payload: dict[str, Any] = {
        "remediation_plan_hash": plan.plan_hash,
        "dry_run_design_report_hash": dry_run_report.report_hash,
        "source_normalization_result_hash": plan.source_normalization_result_hash,
        "lifecycle_boundary_audit_hash": plan.lifecycle_boundary_audit_hash,
        "input_mode": "VERIFIED_LOCAL_PRIMARY_ARCHIVE_ONLY",
        "settled_input_mode": "EVIDENCE_ONLY_NEVER_MERGE",
        "frozen_namespace": "data/normalized/klines",
        "derivative_namespace": "data/normalized/lifecycle_scoped/v0.1.0",
        "output_partition_template": (
            "interval={interval}/symbol={symbol}/year={year}/month={month}/"
            "part-{action_hash}.parquet"
        ),
        "required_checks": _REQUIRED_CHECKS,
        "publication_policy": "STAGE_VALIDATE_ATOMIC_RENAME",
        "resume_policy": "REUSE_IDENTICAL_CONTENT_ONLY",
        "conflict_policy": "FAIL_CLOSED_NEVER_OVERWRITE",
        "empty_partition_policy": "EXCLUSION_RECEIPT_ONLY",
        "action_count": dry_run_report.action_count,
        "expected_source_row_count": dry_run_report.source_row_count,
        "expected_excluded_row_count": dry_run_report.excluded_row_count,
        "expected_retained_row_count": dry_run_report.retained_row_count,
        "frozen_normalization_preserved": True,
        "raw_archive_mutation_authorized": False,
        "executor_implementation_authorized": False,
        "normalization_execution_authorized": False,
        "output_materialized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "status": "BLOCKED",
        "blockers": _BLOCKERS,
    }
    return LifecycleExecutorInterfaceContract.model_validate(
        {**payload, "contract_hash": _hash_payload(payload)}
    )


def write_lifecycle_executor_interface_contract(
    contract: LifecycleExecutorInterfaceContract, data_dir: Path
) -> Path:
    """Publish the immutable non-executing interface contract."""

    contract = LifecycleExecutorInterfaceContract.model_validate(
        contract.model_dump(mode="json")
    )
    destination = (
        data_dir
        / "manifests"
        / "lifecycle_derivative_executor_contract"
        / f"{contract.contract_hash}.json"
    )
    content = canonical_json_bytes(contract.model_dump(mode="json"))
    if destination.is_symlink() or (
        destination.exists() and destination.read_bytes() != content
    ):
        raise LifecycleExecutorContractError("existing executor contract changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
