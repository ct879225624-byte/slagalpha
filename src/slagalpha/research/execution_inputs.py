"""Read-only content verification for every artifact category required by a DEV run."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.parameters import (
    DevParameterVersion,
    require_parameter_plan_binding,
)
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import DatasetRole


class InputArtifactRole(StrEnum):
    """Aggregate artifact categories needed before a formal DEV replay can start."""

    ARCHIVE_MANIFEST = "ARCHIVE_MANIFEST"
    CANDLE_MULTI_TIMEFRAME = "CANDLE_MULTI_TIMEFRAME"
    CANDLE_ONE_MINUTE = "CANDLE_ONE_MINUTE"
    CONTRACT_REGISTRY = "CONTRACT_REGISTRY"
    DEPENDENCY_ARTIFACTS = "DEPENDENCY_ARTIFACTS"
    ENVIRONMENT_LOCK = "ENVIRONMENT_LOCK"
    EXCLUSION_LEDGER = "EXCLUSION_LEDGER"
    FUNDING = "FUNDING"
    PARAMETER_VERSION = "PARAMETER_VERSION"
    RESEARCH_INPUT_AUDIT = "RESEARCH_INPUT_AUDIT"
    RESEARCH_SPLIT = "RESEARCH_SPLIT"
    SENSITIVITY_PLAN = "SENSITIVITY_PLAN"
    STRATEGY_RULES = "STRATEGY_RULES"
    UNIVERSE = "UNIVERSE"
    AGGREGATE_TRADES = "AGGREGATE_TRADES"
    NORMALIZATION_GAP_AUDIT = "NORMALIZATION_GAP_AUDIT"


REQUIRED_INPUT_ROLES = tuple(sorted(
    (role for role in InputArtifactRole if role not in (
        InputArtifactRole.AGGREGATE_TRADES, InputArtifactRole.NORMALIZATION_GAP_AUDIT,
    )),
    key=lambda role: role.value,
))


class InputArtifactSelection(BaseModel):
    """A caller's immutable expected hash for one project-relative input file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: InputArtifactRole
    relative_path: str
    expected_sha256: str

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or path.is_absolute()
            or str(path) != value
            or any(part in ("", ".", "..") for part in path.parts)
            or (path.parts and ":" in path.parts[0])
        ):
            raise ValueError("input path must be a canonical project-relative POSIX path")
        return value

    @field_validator("expected_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("expected input hash must be lowercase SHA-256")
        return value


class VerifiedInputArtifact(InputArtifactSelection):
    """Observed byte length for a selection whose complete file hash matched."""

    size_bytes: int = Field(ge=0)


class DevExecutionInputReport(BaseModel):
    """Content-readiness evidence only; even CHECKS_PASSED is not replay authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-execution-inputs/0.1.0"] = "dev-execution-inputs/0.1.0"
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    sensitivity_plan_hash: str
    parameter_version: str
    artifacts: tuple[VerifiedInputArtifact, ...]
    missing_roles: tuple[InputArtifactRole, ...]
    status: Literal["BLOCKED", "CHECKS_PASSED"]
    blockers: tuple[str, ...]
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    research_authorized: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if len(self.sensitivity_plan_hash) != 64 or any(
            c not in "0123456789abcdef" for c in self.sensitivity_plan_hash
        ):
            raise ValueError("execution plan reference must be lowercase SHA-256")
        prefix = "parameters/0.1.0:"
        parameter_hash = self.parameter_version.removeprefix(prefix)
        if (
            not self.parameter_version.startswith(prefix)
            or len(parameter_hash) != 64
            or any(c not in "0123456789abcdef" for c in parameter_hash)
        ):
            raise ValueError("execution parameter reference is invalid")
        ordered = tuple(sorted(
            self.artifacts, key=lambda item: (item.role.value, item.relative_path)
        ))
        if self.artifacts != ordered or len({
            (item.role, item.relative_path) for item in self.artifacts
        }) != len(self.artifacts):
            raise ValueError("verified input artifacts must be unique and canonical")
        verified_roles = {artifact.role for artifact in self.artifacts}
        expected_missing = tuple(
            role for role in REQUIRED_INPUT_ROLES if role not in verified_roles
        )
        if self.missing_roles != expected_missing:
            raise ValueError("missing input roles do not reconcile with verified artifacts")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("execution input blockers must be unique and canonical")
        if (self.status == "BLOCKED") != bool(self.blockers):
            raise ValueError("execution input status and blockers disagree")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("execution input report content hash mismatch")
        return self


class _InspectionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _inspect_artifact(root: Path, selection: InputArtifactSelection) -> VerifiedInputArtifact:
    try:
        if root.is_symlink():
            raise _InspectionError("UNSAFE_PATH")
        resolved_root = root.resolve(strict=True)
        relative = PurePosixPath(selection.relative_path)
        candidate = root.joinpath(*relative.parts)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise _InspectionError("UNSAFE_PATH")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise _InspectionError("UNSAFE_PATH")
        before = resolved.stat()
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = resolved.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise _InspectionError("CHANGED_DURING_HASH")
        if digest.hexdigest() != selection.expected_sha256:
            raise _InspectionError("HASH_MISMATCH")
        return VerifiedInputArtifact(**selection.model_dump(), size_bytes=after.st_size)
    except _InspectionError:
        raise
    except OSError as error:
        raise _InspectionError("UNAVAILABLE") from error


def inspect_dev_execution_inputs(
    *,
    project_dir: Path,
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    selections: tuple[InputArtifactSelection, ...],
) -> DevExecutionInputReport:
    """Hash complete selected files and preserve every unresolved data blocker."""

    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    require_parameter_plan_binding(parameter, plan)
    selections = tuple(
        InputArtifactSelection.model_validate(item.model_dump()) for item in selections
    )
    if len({(item.role, item.relative_path) for item in selections}) != len(selections):
        raise ValueError("input selections must not contain duplicate role/path pairs")
    artifacts: list[VerifiedInputArtifact] = []
    blockers = list(plan.blockers)
    for selection in selections:
        try:
            artifacts.append(_inspect_artifact(project_dir, selection))
        except _InspectionError as error:
            blockers.append(f"RUN_INPUT_{error.code}_{selection.role.value}")
    ordered_artifacts = tuple(sorted(
        artifacts, key=lambda item: (item.role.value, item.relative_path)
    ))
    verified_roles = {artifact.role for artifact in ordered_artifacts}
    missing = tuple(role for role in REQUIRED_INPUT_ROLES if role not in verified_roles)
    blockers.extend(f"RUN_INPUT_MISSING_{role.value}" for role in missing)
    payload = {
        "schema_version": "dev-execution-inputs/0.1.0",
        "dataset_role": DatasetRole.DEV.value,
        "sensitivity_plan_hash": plan.plan_hash,
        "parameter_version": parameter.parameter_version,
        "artifacts": [artifact.model_dump(mode="json") for artifact in ordered_artifacts],
        "missing_roles": [role.value for role in missing],
        "status": "BLOCKED" if blockers else "CHECKS_PASSED",
        "blockers": sorted(set(blockers)),
        "strategy_executed": False,
        "locked_test_consumed": False,
        "research_authorized": False,
    }
    return DevExecutionInputReport.model_validate({
        **payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    })


def write_dev_execution_input_report(
    report: DevExecutionInputReport, data_dir: Path,
) -> Path:
    """Publish a content-addressed readiness report without overwriting prior evidence."""

    report = DevExecutionInputReport.model_validate(report.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "dev_execution_inputs" / f"{report.report_hash}.json"
    )
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError("execution input report path escapes data directory")
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ValueError("existing execution input report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
