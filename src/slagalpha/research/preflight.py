"""Read-only DEV input/provenance preflight, separate from actual research execution."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.sensitivity import (
    SensitivityPlan,
    SensitivityPlanError,
    build_default_sensitivity_plan,
)
from slagalpha.research.splits import DatasetRole, ResearchInputAuditReport, ResearchSplitManifest


class GitProvenance(BaseModel):
    """Observed project Git state; unavailable status must never become dirty_worktree=False."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code_commit: str | None
    dirty_worktree: bool | None = Field(strict=True)
    inspection_error: Literal["GIT_INSPECTION_FAILED", "GIT_ROOT_MISMATCH"] | None

    @field_validator("code_commit")
    @classmethod
    def validate_commit(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("observed commit must be a full Git object ID")
        return value

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.inspection_error is None and self.dirty_worktree is None:
            raise ValueError("successful Git inspection requires an observed worktree state")
        if self.inspection_error is not None and (
            self.code_commit is not None or self.dirty_worktree is not None
        ):
            raise ValueError("failed Git inspection must not assert known commit/clean state")
        return self


def inspect_git_provenance(project_dir: Path) -> GitProvenance:
    """Read repository identity, HEAD and dirty state; never add/commit or refresh the index."""

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(project_dir), *arguments],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            check=False, timeout=10,
        )

    try:
        top = run("rev-parse", "--show-toplevel")
        if top.returncode:
            raise ValueError("not an accessible repository")
        if Path(top.stdout.strip()).resolve() != project_dir.resolve():
            return GitProvenance(
                code_commit=None, dirty_worktree=None, inspection_error="GIT_ROOT_MISMATCH"
            )
        head = run("rev-parse", "--verify", "HEAD^{commit}")
        status = run(
            "status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=none"
        )
        if status.returncode:
            raise ValueError("Git status unavailable")
        return GitProvenance(
            code_commit=head.stdout.strip() if head.returncode == 0 else None,
            dirty_worktree=bool(status.stdout),
            inspection_error=None,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return GitProvenance(
            code_commit=None, dirty_worktree=None, inspection_error="GIT_INSPECTION_FAILED"
        )


_DEFERRED_CHECKS = (
    "PARAMETER_VERSION_CONTENT_AND_PLAN_BINDING",
    "RUN_INPUT_CONTENT_HASHES",
    "FULL_HISTORICAL_RULE_COVERAGE",
    "ONE_MINUTE_AND_FUNDING_INPUTS",
    "DEPENDENCY_ARTIFACT_HASHES",
)


class DevPreflightReport(BaseModel):
    """A limited prerequisite check, not a completed RunManifest or permission to replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-research-preflight/0.2.0"] = "dev-research-preflight/0.2.0"
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    split_hash: str
    sensitivity_plan_hash: str
    input_audit_hash: str
    git: GitProvenance
    strategy_rules_sha256: str
    environment_lock_hash: str | None
    status: Literal["BLOCKED", "CHECKS_PASSED"]
    blockers: tuple[str, ...]
    deferred_checks: tuple[str, ...] = _DEFERRED_CHECKS
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    research_authorized: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if (self.status == "BLOCKED") != bool(self.blockers):
            raise ValueError("preflight status and blockers disagree")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("preflight blockers must be unique and canonical")
        if self.deferred_checks != _DEFERRED_CHECKS:
            raise ValueError("preflight cannot silently drop unimplemented checks")
        for value in (self.split_hash, self.sensitivity_plan_hash, self.input_audit_hash,
                      self.strategy_rules_sha256, self.environment_lock_hash):
            if value is not None and (
                len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("preflight references must be lowercase SHA-256")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("preflight content hash mismatch")
        return self


def build_dev_preflight_report(
    *,
    split: ResearchSplitManifest,
    plan: SensitivityPlan,
    audit: ResearchInputAuditReport,
    git: GitProvenance,
    strategy_rules_sha256: str,
    environment_lock_hash: str | None,
) -> DevPreflightReport:
    """Check the frozen DEV plan and observed provenance, preserving all data blockers."""

    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    audit = ResearchInputAuditReport.model_validate(audit.model_dump(mode="json"))
    git = GitProvenance.model_validate(git.model_dump(mode="json"))
    expected_plan = build_default_sensitivity_plan(
        split=split, audit=audit, strategy_rules_sha256=plan.strategy_rules_sha256
    )
    if expected_plan != plan:
        raise SensitivityPlanError("preflight plan does not match the frozen split and audit")
    blockers = list(plan.blockers)
    if git.inspection_error is not None:
        blockers.append(git.inspection_error)
    else:
        if git.code_commit is None:
            blockers.append("GIT_COMMIT_MISSING")
        if git.dirty_worktree:
            blockers.append("WORKTREE_DIRTY")
    if environment_lock_hash is None:
        blockers.append("ENVIRONMENT_LOCK_MISSING")
    if strategy_rules_sha256 != plan.strategy_rules_sha256:
        blockers.append("STRATEGY_RULES_CHANGED")
    payload = {
        "schema_version": "dev-research-preflight/0.2.0",
        "dataset_role": DatasetRole.DEV.value,
        "split_hash": split.split_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "input_audit_hash": audit.report_hash,
        "git": git.model_dump(mode="json"),
        "strategy_rules_sha256": strategy_rules_sha256,
        "environment_lock_hash": environment_lock_hash,
        "status": "BLOCKED" if blockers else "CHECKS_PASSED",
        "blockers": sorted(set(blockers)),
        "deferred_checks": list(_DEFERRED_CHECKS),
        "strategy_executed": False,
        "locked_test_consumed": False,
        "research_authorized": False,
    }
    return DevPreflightReport.model_validate({
        **payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    })


def write_dev_preflight_report(report: DevPreflightReport, data_dir: Path) -> Path:
    report = DevPreflightReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests" / "dev_preflight" / f"{report.report_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError("preflight path escapes data directory")
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ValueError("existing preflight report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
