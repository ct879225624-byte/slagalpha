"""Semantic and cross-reference checks layered on verified DEV input file bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, model_validator

from slagalpha.data.archive_batch import ArchiveBatchResult
from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.data.universe_batch import UniverseBatchResult
from slagalpha.domain.universe import ContractRegistry, ExclusionLedger
from slagalpha.reporting.dependency_artifacts import (
    DependencyArtifactManifest,
    inspect_dependency_artifacts,
)
from slagalpha.reporting.environment import EnvironmentLockError, inspect_environment_lock
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.execution_inputs import (
    REQUIRED_INPUT_ROLES,
    DevExecutionInputReport,
    InputArtifactRole,
    InputArtifactSelection,
    VerifiedInputArtifact,
    inspect_dev_execution_inputs,
)
from slagalpha.research.normalization_gaps import (
    FINITE_LOOKBACK_BARS,
    NormalizationGapAuditReport,
)
from slagalpha.research.parameters import (
    DevParameterVersion,
    require_parameter_plan_binding,
)
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import (
    DatasetRole,
    ResearchInputAuditReport,
    ResearchSplitManifest,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class DevExecutionSemanticReport(BaseModel):
    """Parsed manifest readiness; never permission to run a replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-execution-semantics/0.1.0"] = (
        "dev-execution-semantics/0.1.0"
    )
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    content_report_hash: str
    validated_roles: tuple[InputArtifactRole, ...]
    deferred_roles: tuple[InputArtifactRole, ...]
    status: Literal["BLOCKED", "CHECKS_PASSED"]
    blockers: tuple[str, ...]
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    research_authorized: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        for value in (self.content_report_hash, self.report_hash):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("semantic report references must be lowercase SHA-256")
        ordered_validated = tuple(sorted(self.validated_roles, key=lambda role: role.value))
        ordered_deferred = tuple(sorted(self.deferred_roles, key=lambda role: role.value))
        if self.validated_roles != ordered_validated or len(set(self.validated_roles)) != len(
            self.validated_roles
        ):
            raise ValueError("validated semantic roles must be unique and canonical")
        if self.deferred_roles != ordered_deferred or len(set(self.deferred_roles)) != len(
            self.deferred_roles
        ):
            raise ValueError("deferred semantic roles must be unique and canonical")
        if set(self.validated_roles) & set(self.deferred_roles):
            raise ValueError("semantic roles cannot be both validated and deferred")
        if set(self.validated_roles) | set(self.deferred_roles) != set(REQUIRED_INPUT_ROLES):
            raise ValueError("semantic roles must account for every required input category")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("semantic blockers must be unique and canonical")
        if (self.status == "BLOCKED") != bool(self.blockers):
            raise ValueError("semantic status and blockers disagree")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("semantic report content hash mismatch")
        return self


def _read_verified(project_dir: Path, artifact: VerifiedInputArtifact) -> bytes:
    path = project_dir.joinpath(*Path(artifact.relative_path).parts)
    if path.is_symlink():
        raise ValueError("verified semantic input became a symlink")
    content = path.read_bytes()
    if len(content) != artifact.size_bytes or hashlib.sha256(content).hexdigest() != (
        artifact.expected_sha256
    ):
        raise ValueError("verified semantic input changed")
    return content


def inspect_dev_execution_semantics(
    *,
    project_dir: Path,
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    content_report: DevExecutionInputReport,
) -> DevExecutionSemanticReport:
    """Parse known manifests, cross-bind them, and preserve all content/data blockers."""

    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    require_parameter_plan_binding(parameter, plan)
    content_report = DevExecutionInputReport.model_validate(
        content_report.model_dump(mode="json")
    )
    selections = tuple(InputArtifactSelection(
        role=artifact.role,
        relative_path=artifact.relative_path,
        expected_sha256=artifact.expected_sha256,
    ) for artifact in content_report.artifacts)
    refreshed = inspect_dev_execution_inputs(
        project_dir=project_dir, plan=plan, parameter=parameter, selections=selections
    )
    if refreshed != content_report:
        raise ValueError("content report no longer matches current input files")

    by_role: dict[InputArtifactRole, list[VerifiedInputArtifact]] = {}
    for artifact in content_report.artifacts:
        by_role.setdefault(artifact.role, []).append(artifact)
    blockers = list(content_report.blockers)
    validated: set[InputArtifactRole] = set()

    def one(role: InputArtifactRole) -> VerifiedInputArtifact | None:
        matches = by_role.get(role, [])
        if len(matches) > 1:
            blockers.append(f"RUN_INPUT_SEMANTIC_MULTIPLE_{role.value}")
            return None
        return matches[0] if matches else None

    def parse(role: InputArtifactRole, model: type[ModelT]) -> ModelT | None:
        artifact = one(role)
        if artifact is None:
            return None
        try:
            return model.model_validate_json(_read_verified(project_dir, artifact))
        except (OSError, ValueError):
            blockers.append(f"RUN_INPUT_SEMANTIC_INVALID_{role.value}")
            return None

    strategy = one(InputArtifactRole.STRATEGY_RULES)
    if strategy is not None:
        if strategy.expected_sha256 == plan.strategy_rules_sha256:
            validated.add(InputArtifactRole.STRATEGY_RULES)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_STRATEGY_RULES")

    environment = one(InputArtifactRole.ENVIRONMENT_LOCK)
    if environment is not None:
        try:
            observed_lock_hash = inspect_environment_lock(
                project_dir.joinpath(*Path(environment.relative_path).parts)
            )
        except (EnvironmentLockError, OSError):
            blockers.append("RUN_INPUT_SEMANTIC_INVALID_ENVIRONMENT_LOCK")
        else:
            if observed_lock_hash == environment.expected_sha256:
                validated.add(InputArtifactRole.ENVIRONMENT_LOCK)
            else:
                blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_ENVIRONMENT_LOCK")

    saved_plan = parse(InputArtifactRole.SENSITIVITY_PLAN, SensitivityPlan)
    if saved_plan is not None:
        if saved_plan == plan:
            validated.add(InputArtifactRole.SENSITIVITY_PLAN)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_SENSITIVITY_PLAN")

    saved_parameter = parse(InputArtifactRole.PARAMETER_VERSION, DevParameterVersion)
    if saved_parameter is not None:
        try:
            require_parameter_plan_binding(saved_parameter, plan)
        except ValueError:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_PARAMETER_VERSION")
        else:
            if saved_parameter == parameter:
                validated.add(InputArtifactRole.PARAMETER_VERSION)
            else:
                blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_PARAMETER_VERSION")

    split = parse(InputArtifactRole.RESEARCH_SPLIT, ResearchSplitManifest)
    if split is not None:
        if split.split_hash == plan.split_hash:
            validated.add(InputArtifactRole.RESEARCH_SPLIT)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_RESEARCH_SPLIT")

    audit = parse(InputArtifactRole.RESEARCH_INPUT_AUDIT, ResearchInputAuditReport)
    if audit is not None:
        audit_matches = (
            audit.report_hash == plan.input_audit_hash
            and audit.contract_registry_version == plan.contract_registry_version
            and (split is None or (
                audit.split_hash == split.split_hash
                and audit.daily_snapshot_hash == split.daily_snapshot_hash
            ))
        )
        if audit_matches:
            validated.add(InputArtifactRole.RESEARCH_INPUT_AUDIT)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_RESEARCH_INPUT_AUDIT")

    registry = parse(InputArtifactRole.CONTRACT_REGISTRY, ContractRegistry)
    if registry is not None:
        if registry.registry_version == plan.contract_registry_version:
            validated.add(InputArtifactRole.CONTRACT_REGISTRY)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_CONTRACT_REGISTRY")

    universe = parse(InputArtifactRole.UNIVERSE, UniverseBatchResult)
    if universe is not None:
        universe_matches = universe.complete and (
            split is None or (
                universe.run_version == split.universe_batch_run_version
                and universe.daily_snapshot_hash == split.daily_snapshot_hash
                and universe.selection_start == split.segments[0].start
                and universe.selection_end_exclusive == split.segments[-1].end_exclusive
            )
        )
        if universe_matches:
            validated.add(InputArtifactRole.UNIVERSE)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_UNIVERSE")

    normalization = parse(
        InputArtifactRole.CANDLE_MULTI_TIMEFRAME, NormalizationBatchResult
    )
    if normalization is not None:
        normalization_matches = normalization.complete and (
            universe is None or normalization.dataset_content_hash == universe.dataset_content_hash
        )
        if normalization_matches:
            validated.add(InputArtifactRole.CANDLE_MULTI_TIMEFRAME)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_CANDLE_MULTI_TIMEFRAME")

    gap_audit = parse(InputArtifactRole.NORMALIZATION_GAP_AUDIT, NormalizationGapAuditReport)
    if gap_audit is not None:
        gap_matches = (
            normalization is not None and universe is not None and split is not None
            and gap_audit.normalization_result_hash == normalization.result_hash
            and gap_audit.daily_snapshot_hash == universe.daily_snapshot_hash
            and gap_audit.daily_snapshot_hash == split.daily_snapshot_hash
            and gap_audit.snapshot_count == universe.expected_count
            and gap_audit.failure_file_count == normalization.failed_count
            and gap_audit.finite_lookback_bars == FINITE_LOOKBACK_BARS
            and {item.evidence.identity for item in gap_audit.dependencies}.issubset(
                {item.identity for item in normalization.failures}
            )
        )
        if not gap_matches:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_NORMALIZATION_GAP_AUDIT")
        else:
            blockers.extend(f"RUN_INPUT_GAP_AUDIT:{code}" for code in gap_audit.blockers)
        # Diagnostic evidence never overrides the complete-normalization requirement.

    archive = parse(InputArtifactRole.ARCHIVE_MANIFEST, ArchiveBatchResult)
    if archive is not None:
        archive_matches = archive.complete and (
            normalization is None or archive.plan_content_hash == normalization.plan_content_hash
        )
        if archive_matches:
            validated.add(InputArtifactRole.ARCHIVE_MANIFEST)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_ARCHIVE_MANIFEST")

    ledger = parse(InputArtifactRole.EXCLUSION_LEDGER, ExclusionLedger)
    if ledger is not None:
        if universe is None or ledger.ledger_version == universe.exclusion_ledger_version:
            validated.add(InputArtifactRole.EXCLUSION_LEDGER)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_MISMATCH_EXCLUSION_LEDGER")

    dependencies = parse(InputArtifactRole.DEPENDENCY_ARTIFACTS, DependencyArtifactManifest)
    if dependencies is not None:
        if environment is None or InputArtifactRole.ENVIRONMENT_LOCK not in validated:
            blockers.append("RUN_INPUT_SEMANTIC_UNBOUND_DEPENDENCY_ARTIFACTS")
        else:
            try:
                inspect_dependency_artifacts(
                    project_dir=project_dir,
                    environment_lock=_read_verified(project_dir, environment),
                    manifest=dependencies,
                )
            except (OSError, ValueError):
                blockers.append("RUN_INPUT_SEMANTIC_INVALID_DEPENDENCY_ARTIFACTS")
            else:
                validated.add(InputArtifactRole.DEPENDENCY_ARTIFACTS)

    for role in (
        InputArtifactRole.CANDLE_ONE_MINUTE,
        InputArtifactRole.FUNDING,
    ):
        if one(role) is not None:
            blockers.append(f"RUN_INPUT_SEMANTIC_VALIDATOR_MISSING_{role.value}")
    if one(InputArtifactRole.AGGREGATE_TRADES) is not None:
        blockers.append("RUN_INPUT_SEMANTIC_VALIDATOR_MISSING_AGGREGATE_TRADES")

    validated_roles = tuple(sorted(validated, key=lambda role: role.value))
    deferred_roles = tuple(
        role for role in REQUIRED_INPUT_ROLES if role not in validated
    )
    payload: dict[str, Any] = {
        "schema_version": "dev-execution-semantics/0.1.0",
        "dataset_role": DatasetRole.DEV.value,
        "content_report_hash": content_report.report_hash,
        "validated_roles": [role.value for role in validated_roles],
        "deferred_roles": [role.value for role in deferred_roles],
        "status": "BLOCKED" if blockers else "CHECKS_PASSED",
        "blockers": sorted(set(blockers)),
        "strategy_executed": False,
        "locked_test_consumed": False,
        "research_authorized": False,
    }
    return DevExecutionSemanticReport.model_validate({
        **payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    })


def write_dev_execution_semantic_report(
    report: DevExecutionSemanticReport, data_dir: Path,
) -> Path:
    report = DevExecutionSemanticReport.model_validate(report.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "dev_execution_semantics" / f"{report.report_hash}.json"
    )
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError("semantic report path escapes data directory")
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ValueError("existing semantic report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
