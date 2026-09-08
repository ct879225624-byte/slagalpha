"""Audit the design boundary for a future real lifecycle derivative dry-run."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FROZEN_NAMESPACE = PurePosixPath("data/normalized/klines")
_DERIVATIVE_NAMESPACE = PurePosixPath(
    "data/normalized/lifecycle_scoped/v0.1.0"
)


class LifecycleDryRunDesignError(ValueError):
    """Raised when a real-input dry-run design is not isolated or complete."""


def _require_sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("lifecycle dry-run references must be lowercase SHA-256")
    return value


def _namespace_isolated(frozen: PurePosixPath, derivative: PurePosixPath) -> bool:
    return (
        frozen != derivative
        and frozen not in derivative.parents
        and derivative not in frozen.parents
    )


def _report_hash(payload: dict[str, Any]) -> str:
    candidate = LifecycleDryRunDesignReport.model_construct(
        **payload, report_hash="0" * 64
    )
    canonical = candidate.model_dump(mode="json", exclude={"report_hash"})
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


class LifecycleDryRunDesignReport(BaseModel):
    """Content-addressed, non-executing audit of real-input materialization design."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-derivative-dry-run-design/0.1.0"] = (
        "lifecycle-derivative-dry-run-design/0.1.0"
    )
    remediation_plan_hash: str
    normalization_result_hash: str
    lifecycle_boundary_audit_hash: str
    frozen_normalization_namespace: str
    derivative_normalization_namespace: str
    namespace_isolated: Literal[True] = True
    action_count: int = Field(gt=0)
    lineage_complete_action_count: int = Field(ge=0)
    source_row_count: int = Field(ge=0)
    excluded_row_count: int = Field(ge=0)
    retained_row_count: int = Field(ge=0)
    settled_evidence_row_count: int = Field(ge=0)
    input_rows_conserved: Literal[True] = True
    source_manifest_read_only: Literal[True] = True
    raw_market_data_read: Literal[False] = False
    derivative_executor_called: Literal[False] = False
    output_materialized: Literal[False] = False
    normalization_execution_authorized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    status: Literal["BLOCKED"] = "BLOCKED"
    blockers: tuple[str, ...] = Field(min_length=1)
    report_hash: str

    @field_validator(
        "remediation_plan_hash",
        "normalization_result_hash",
        "lifecycle_boundary_audit_hash",
        "report_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.frozen_normalization_namespace != str(_FROZEN_NAMESPACE):
            raise ValueError("frozen normalization namespace is not canonical")
        if self.derivative_normalization_namespace != str(_DERIVATIVE_NAMESPACE):
            raise ValueError("derivative normalization namespace is not canonical")
        if not self.namespace_isolated:
            raise ValueError("normalization namespaces must remain isolated")
        if self.lineage_complete_action_count != self.action_count:
            raise ValueError("not every remediation action has complete lineage")
        if self.excluded_row_count + self.retained_row_count != self.source_row_count:
            raise ValueError("dry-run input rows are not conserved")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("dry-run blockers must be unique and canonical")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("lifecycle dry-run design report hash mismatch")
        return self


def build_lifecycle_dry_run_design_report(
    *,
    plan: LifecycleNormalizationRemediationPlan,
    expected_plan_hash: str,
    expected_normalization_result_hash: str,
    expected_lifecycle_boundary_audit_hash: str,
    frozen_normalization_namespace: str = str(_FROZEN_NAMESPACE),
    derivative_normalization_namespace: str = str(_DERIVATIVE_NAMESPACE),
) -> LifecycleDryRunDesignReport:
    """Validate only manifest design inputs; never read market files or execute normalization."""

    try:
        plan = LifecycleNormalizationRemediationPlan.model_validate(
            plan.model_dump(mode="json")
        )
        _require_sha256(expected_plan_hash)
        _require_sha256(expected_normalization_result_hash)
        _require_sha256(expected_lifecycle_boundary_audit_hash)
    except ValueError as error:
        raise LifecycleDryRunDesignError("dry-run design source is invalid") from error
    if plan.plan_hash != expected_plan_hash:
        raise LifecycleDryRunDesignError("unexpected remediation plan")
    if plan.source_normalization_result_hash != expected_normalization_result_hash:
        raise LifecycleDryRunDesignError("unexpected frozen normalization result")
    if plan.lifecycle_boundary_audit_hash != expected_lifecycle_boundary_audit_hash:
        raise LifecycleDryRunDesignError("unexpected lifecycle boundary audit")
    frozen = PurePosixPath(frozen_normalization_namespace)
    derivative = PurePosixPath(derivative_normalization_namespace)
    if not _namespace_isolated(frozen, derivative):
        raise LifecycleDryRunDesignError("frozen and derivative namespaces collide")
    lineage_complete = sum(
        bool(
            action.primary_archive_sha256
            and action.primary_rows_sha256
            and action.settled_archive_sha256
            and action.settled_rows_sha256
            and action.identity_source_ref
            and action.settled_symbol
        )
        for action in plan.actions
    )
    source_rows = sum(action.boundary_month_source_row_count for action in plan.actions)
    excluded_rows = sum(action.boundary_month_excluded_row_count for action in plan.actions)
    retained_rows = sum(
        action.boundary_month_expected_retained_row_count for action in plan.actions
    )
    blockers = tuple(
        sorted(
            {
                "REAL_DERIVATIVE_EXECUTION_NOT_AUTHORIZED",
                "FROZEN_NORMALIZATION_MUST_REMAIN_UNCHANGED",
                "HISTORICAL_RULE_GATE_REMAINS_BLOCKED",
                "RESEARCH_AUTHORIZATION_REMAINS_FALSE",
            }
        )
    )
    payload: dict[str, Any] = {
        "remediation_plan_hash": plan.plan_hash,
        "normalization_result_hash": plan.source_normalization_result_hash,
        "lifecycle_boundary_audit_hash": plan.lifecycle_boundary_audit_hash,
        "frozen_normalization_namespace": str(frozen),
        "derivative_normalization_namespace": str(derivative),
        "namespace_isolated": True,
        "action_count": plan.action_count,
        "lineage_complete_action_count": lineage_complete,
        "source_row_count": source_rows,
        "excluded_row_count": excluded_rows,
        "retained_row_count": retained_rows,
        "settled_evidence_row_count": plan.settled_evidence_row_count,
        "input_rows_conserved": True,
        "source_manifest_read_only": True,
        "raw_market_data_read": False,
        "derivative_executor_called": False,
        "output_materialized": False,
        "normalization_execution_authorized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "status": "BLOCKED",
        "blockers": blockers,
    }
    return LifecycleDryRunDesignReport.model_validate(
        {
            **payload,
                "report_hash": _report_hash(payload),
        }
    )


def write_lifecycle_dry_run_design_report(
    report: LifecycleDryRunDesignReport, data_dir: Path
) -> Path:
    """Publish one immutable design report; no derivative output is written."""

    report = LifecycleDryRunDesignReport.model_validate(report.model_dump(mode="json"))
    destination = (
        data_dir
        / "manifests"
        / "lifecycle_derivative_dry_run_design"
        / f"{report.report_hash}.json"
    )
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (
        destination.exists() and destination.read_bytes() != content
    ):
        raise LifecycleDryRunDesignError("existing dry-run design report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
