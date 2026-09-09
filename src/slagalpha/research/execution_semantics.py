"""Semantic and cross-reference checks layered on verified DEV input file bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

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
from slagalpha.research.lifecycle_real_execution import LifecycleRealExecutionReceipt
from slagalpha.research.lifecycle_replacement import (
    LifecycleReplacementNormalizationResult,
)
from slagalpha.research.normalization_gaps import (
    FINITE_LOOKBACK_BARS,
    NormalizationGapAuditReport,
)
from slagalpha.research.parameters import (
    DevParameterVersion,
    require_parameter_plan_binding,
)
from slagalpha.research.replay_market_data import ReplayMarketDataArtifact
from slagalpha.research.request_set_market_data import DevRequestSetMarketDataReport
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import (
    DatasetRole,
    ResearchInputAuditReport,
    ResearchSplitManifest,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class LifecycleReplacementSemanticReference(BaseModel):
    """Verified local lifecycle overlay and its still-blocked coverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_hash: str
    dataset_content_hash: str
    real_execution_receipt_hash: str
    requested_partition_count: int = Field(gt=0)
    available_partition_count: int = Field(ge=0)
    unavailable_partition_count: int = Field(ge=0)
    verified_output_count: int = Field(gt=0)
    verified_output_bytes: int = Field(gt=0)
    status: Literal["BLOCKED"] = "BLOCKED"
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        for value in (
            self.result_hash,
            self.dataset_content_hash,
            self.real_execution_receipt_hash,
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("replacement semantic references must be lowercase SHA-256")
        if self.available_partition_count + self.unavailable_partition_count != (
            self.requested_partition_count
        ):
            raise ValueError("replacement semantic partition counts do not reconcile")
        if not self.blockers or self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("replacement semantic blockers must be non-empty and canonical")
        return self


class DevExecutionSemanticReport(BaseModel):
    """Parsed manifest readiness; never permission to run a replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-execution-semantics/0.1.0", "dev-execution-semantics/0.2.0"] = (
        "dev-execution-semantics/0.1.0"
    )
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    content_report_hash: str
    validated_roles: tuple[InputArtifactRole, ...]
    deferred_roles: tuple[InputArtifactRole, ...]
    status: Literal["BLOCKED", "CHECKS_PASSED"]
    blockers: tuple[str, ...]
    replacement_lineage: LifecycleReplacementSemanticReference | None = None
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
        if self.schema_version == "dev-execution-semantics/0.1.0":
            if self.replacement_lineage is not None:
                raise ValueError("v0.1 semantic reports cannot contain replacement lineage")
        elif self.replacement_lineage is None and (
            "RUN_INPUT_SEMANTIC_INVALID_LIFECYCLE_REPLACEMENT" not in self.blockers
        ):
            raise ValueError("v0.2 semantic reports must account for replacement lineage")
        payload = self.model_dump(mode="json", exclude={"report_hash"}, exclude_none=True)
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


def _verify_lifecycle_replacement(
    *,
    project_dir: Path,
    normalization: NormalizationBatchResult,
    replacement: LifecycleReplacementNormalizationResult,
) -> LifecycleReplacementSemanticReference | None:
    """Revalidate the completion receipt and every local derivative byte."""

    root = project_dir.resolve()
    output_root = (root / "data/normalized/lifecycle_scoped/v0.1.0").resolve()
    receipt_path = (
        root
        / "data/manifests/lifecycle_real_execution"
        / f"{replacement.real_execution_receipt_hash}.json"
    )
    try:
        if not output_root.is_relative_to(root) or receipt_path.is_symlink():
            return None
        receipt = LifecycleRealExecutionReceipt.model_validate_json(receipt_path.read_bytes())
        if (
            receipt.receipt_hash != replacement.real_execution_receipt_hash
            or receipt.remediation_plan_hash != replacement.remediation_plan_hash
            or receipt.source_normalization_result_hash != normalization.result_hash
            or replacement.source_normalization_result_hash != normalization.result_hash
            or replacement.source_dataset_content_hash != normalization.dataset_content_hash
            or replacement.requested_partition_count != normalization.requested_count
            or replacement.materialized_overlay_partition_count != receipt.materialized_action_count
            or replacement.excluded_overlay_partition_count != receipt.excluded_action_count
            or replacement.derivative_retained_row_count != receipt.retained_row_count
        ):
            return None
        output_bytes = 0
        for output in receipt.outputs:
            relative = Path(output.output_relative_path)
            if relative.is_absolute():
                return None
            path = root.joinpath(*relative.parts)
            resolved = path.resolve()
            if not resolved.is_relative_to(output_root) or path.is_symlink() or not path.is_file():
                return None
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != output.output_sha256:
                return None
            output_bytes += len(content)
    except (OSError, ValueError):
        return None
    return LifecycleReplacementSemanticReference(
        result_hash=replacement.result_hash,
        dataset_content_hash=replacement.replacement_dataset_content_hash,
        real_execution_receipt_hash=receipt.receipt_hash,
        requested_partition_count=replacement.requested_partition_count,
        available_partition_count=replacement.replacement_available_partition_count,
        unavailable_partition_count=replacement.unavailable_partition_count,
        verified_output_count=len(receipt.outputs),
        verified_output_bytes=output_bytes,
        status=replacement.status,
        blockers=replacement.blockers,
    )


def inspect_dev_execution_semantics(
    *,
    project_dir: Path,
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    content_report: DevExecutionInputReport,
    replacement: LifecycleReplacementNormalizationResult | None = None,
    expected_replacement_hash: str | None = None,
) -> DevExecutionSemanticReport:
    """Parse known manifests, cross-bind them, and preserve all content/data blockers."""

    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    require_parameter_plan_binding(parameter, plan)
    content_report = DevExecutionInputReport.model_validate(content_report.model_dump(mode="json"))
    if (replacement is None) != (expected_replacement_hash is None):
        raise ValueError("replacement and its expected hash must be provided together")
    if replacement is not None:
        replacement = LifecycleReplacementNormalizationResult.model_validate(
            replacement.model_dump(mode="json")
        )
        if replacement.result_hash != expected_replacement_hash:
            raise ValueError("unexpected lifecycle replacement input")
    selections = tuple(
        InputArtifactSelection(
            role=artifact.role,
            relative_path=artifact.relative_path,
            expected_sha256=artifact.expected_sha256,
        )
        for artifact in content_report.artifacts
    )
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
            and (
                split is None
                or (
                    audit.split_hash == split.split_hash
                    and audit.daily_snapshot_hash == split.daily_snapshot_hash
                )
            )
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
            split is None
            or (
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

    normalization = parse(InputArtifactRole.CANDLE_MULTI_TIMEFRAME, NormalizationBatchResult)
    replacement_lineage: LifecycleReplacementSemanticReference | None = None
    if normalization is not None and replacement is not None:
        replacement_lineage = _verify_lifecycle_replacement(
            project_dir=project_dir,
            normalization=normalization,
            replacement=replacement,
        )
        if replacement_lineage is None:
            blockers.append("RUN_INPUT_SEMANTIC_INVALID_LIFECYCLE_REPLACEMENT")
    if normalization is not None:
        normalization_matches = normalization.complete and (
            universe is None or normalization.dataset_content_hash == universe.dataset_content_hash
        )
        if normalization_matches:
            validated.add(InputArtifactRole.CANDLE_MULTI_TIMEFRAME)
        elif (
            replacement_lineage is not None
            and replacement is not None
            and (
                universe is None
                or replacement.source_dataset_content_hash == universe.dataset_content_hash
            )
        ):
            blockers.extend(
                f"RUN_INPUT_REPLACEMENT:{blocker}" for blocker in replacement_lineage.blockers
            )
            if replacement_lineage.unavailable_partition_count:
                blockers.append(
                    "RUN_INPUT_SEMANTIC_INCOMPLETE_LIFECYCLE_REPLACEMENT_CANDLE_MULTI_TIMEFRAME"
                )
            else:
                validated.add(InputArtifactRole.CANDLE_MULTI_TIMEFRAME)
        else:
            blockers.append("RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_CANDLE_MULTI_TIMEFRAME")

    gap_audit = parse(InputArtifactRole.NORMALIZATION_GAP_AUDIT, NormalizationGapAuditReport)
    if gap_audit is not None:
        gap_matches = (
            normalization is not None
            and universe is not None
            and split is not None
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
        elif replacement_lineage is None:
            blockers.extend(f"RUN_INPUT_GAP_AUDIT:{code}" for code in gap_audit.blockers)
        # A verified replacement supersedes old lifecycle-gap diagnostics but not its blockers.

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
        replay_artifact = one(role)
        if replay_artifact is None:
            continue
        try:
            replay_data: ReplayMarketDataArtifact | DevRequestSetMarketDataReport = TypeAdapter(
                ReplayMarketDataArtifact | DevRequestSetMarketDataReport
            ).validate_json(_read_verified(project_dir, replay_artifact))
        except (OSError, ValueError):
            blockers.append(f"RUN_INPUT_SEMANTIC_INVALID_{role.value}")
            continue
        if isinstance(replay_data, DevRequestSetMarketDataReport):
            # A saved receipt alone cannot re-establish the current full source chain,
            # nor does it prove that its scan declarations came from a verified strategy run.
            blockers.append(
                f"RUN_INPUT_SEMANTIC_REQUEST_SET_SOURCE_REVALIDATION_REQUIRED_{role.value}"
            )
        elif replay_data.role != role.value:
            blockers.append(f"RUN_INPUT_SEMANTIC_MISMATCH_{role.value}")
        else:
            # One bounded request never establishes the full DEV request set's coverage.
            blockers.append(f"RUN_INPUT_SEMANTIC_REQUEST_SET_COVERAGE_REQUIRED_{role.value}")
    if one(InputArtifactRole.AGGREGATE_TRADES) is not None:
        blockers.append("RUN_INPUT_SEMANTIC_VALIDATOR_MISSING_AGGREGATE_TRADES")

    validated_roles = tuple(sorted(validated, key=lambda role: role.value))
    deferred_roles = tuple(role for role in REQUIRED_INPUT_ROLES if role not in validated)
    payload: dict[str, Any] = {
        "schema_version": (
            "dev-execution-semantics/0.2.0"
            if replacement is not None
            else "dev-execution-semantics/0.1.0"
        ),
        "dataset_role": DatasetRole.DEV.value,
        "content_report_hash": content_report.report_hash,
        "validated_roles": [role.value for role in validated_roles],
        "deferred_roles": [role.value for role in deferred_roles],
        "status": "BLOCKED" if blockers else "CHECKS_PASSED",
        "blockers": sorted(set(blockers)),
        "replacement_lineage": (
            replacement_lineage.model_dump(mode="json") if replacement_lineage is not None else None
        ),
        "strategy_executed": False,
        "locked_test_consumed": False,
        "research_authorized": False,
    }
    hash_payload = {key: value for key, value in payload.items() if value is not None}
    return DevExecutionSemanticReport.model_validate(
        {
            **hash_payload,
            "report_hash": hashlib.sha256(canonical_json_bytes(hash_payload)).hexdigest(),
        }
    )


def write_dev_execution_semantic_report(
    report: DevExecutionSemanticReport,
    data_dir: Path,
) -> Path:
    report = DevExecutionSemanticReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests" / "dev_execution_semantics" / f"{report.report_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError("semantic report path escapes data directory")
    content = canonical_json_bytes(report.model_dump(mode="json", exclude_none=True))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ValueError("existing semantic report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
