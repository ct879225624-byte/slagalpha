"""Synthetic readiness tests plus isolated temporary Git repositories, never project commits."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from slagalpha.research.preflight import (
    DevPreflightReport,
    GitProvenance,
    build_dev_preflight_report,
    inspect_git_provenance,
    write_dev_preflight_report,
)
from slagalpha.research.sensitivity import SensitivityPlanError
from test_sensitivity_plan import _audit, _plan, _split


def _git_inputs(**updates: Any) -> GitProvenance:
    return GitProvenance.model_validate({
        "code_commit": "a" * 40, "dirty_worktree": False, "inspection_error": None,
        **updates,
    })


def _report(*, ready: bool = True, **updates: Any) -> DevPreflightReport:
    plan = _plan(ready=ready)
    return build_dev_preflight_report(**{
        "split": _split(), "plan": plan, "audit": _audit(ready=ready),
        "git": _git_inputs(), "environment_lock_hash": "b" * 64,
        "strategy_rules_sha256": plan.strategy_rules_sha256,
        **updates,
    })


def test_checks_passing_does_not_authorize_research_or_consume_locked_test(tmp_path: Path) -> None:
    report = _report()
    assert report.status == "CHECKS_PASSED"
    assert report.blockers == ()
    assert report.strategy_executed is report.locked_test_consumed is False
    assert report.research_authorized is False
    assert report == _report()
    path = write_dev_preflight_report(report, tmp_path)
    assert DevPreflightReport.model_validate_json(path.read_bytes()) == report
    assert write_dev_preflight_report(report, tmp_path) == path
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        write_dev_preflight_report(report, tmp_path)


def test_all_known_blockers_are_reported_together() -> None:
    report = _report(
        ready=False, git=_git_inputs(code_commit=None, dirty_worktree=True),
        environment_lock_hash=None,
    )
    assert report.status == "BLOCKED"
    assert report.blockers == (
        "ENVIRONMENT_LOCK_MISSING", "GIT_COMMIT_MISSING",
        "NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS", "WORKTREE_DIRTY",
    )


def test_unknown_git_state_cannot_be_interpreted_as_clean() -> None:
    report = _report(git=GitProvenance(
        code_commit=None, dirty_worktree=None, inspection_error="GIT_INSPECTION_FAILED"
    ))
    assert report.blockers == ("GIT_INSPECTION_FAILED",)
    with pytest.raises(ValueError):
        _git_inputs(dirty_worktree=None)


def test_changed_strategy_document_blocks_without_rewriting_frozen_plan() -> None:
    plan = _plan(ready=True)
    original = plan.model_dump_json()
    report = _report(plan=plan, strategy_rules_sha256="c" * 64)
    assert report.blockers == ("STRATEGY_RULES_CHANGED",)
    assert plan.model_dump_json() == original


def test_plan_from_different_audit_is_rejected_not_treated_as_ready() -> None:
    with pytest.raises(SensitivityPlanError, match="does not match"):
        _report(ready=True, plan=_plan(ready=False))


def test_tampered_preflight_cannot_reuse_old_hash_or_enable_execution() -> None:
    report = _report()
    changes: tuple[dict[str, Any], ...] = (
        {"research_authorized": True}, {"strategy_executed": True},
        {"dataset_role": "LOCKED_TEST"}, {"environment_lock_hash": "d" * 64},
    )
    for update in changes:
        with pytest.raises(ValueError):
            DevPreflightReport.model_validate({**report.model_dump(), **update})


def test_real_git_inspection_handles_unborn_clean_dirty_and_wrong_root(tmp_path: Path) -> None:
    # All mutating commands below target this disposable test repository only.
    project = tmp_path / "fixture-repo"
    project.mkdir()
    environment = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        environment.pop(key, None)
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_COUNT": "0",
    })

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(project), *arguments], env=environment,
            check=True, capture_output=True, timeout=10,
        )

    git("-c", "init.templateDir=", "init")
    unborn = inspect_git_provenance(project)
    assert unborn.code_commit is None
    assert unborn.dirty_worktree is False
    (project / "tracked.txt").write_text("fixture", encoding="utf-8")
    git("add", "tracked.txt")
    git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", "Synthetic test fixture",
    )
    clean = inspect_git_provenance(project)
    assert clean.inspection_error is None
    assert clean.code_commit is not None and len(clean.code_commit) == 40
    assert clean.dirty_worktree is False
    (project / "untracked.txt").write_text("fixture", encoding="utf-8")
    assert inspect_git_provenance(project).dirty_worktree is True
    nested = project / "nested"
    nested.mkdir()
    assert inspect_git_provenance(nested).inspection_error == "GIT_ROOT_MISMATCH"


def test_git_failure_is_fail_closed_and_does_not_expose_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(["git", "sensitive-test-marker"], 10)

    monkeypatch.setattr(subprocess, "run", fail)
    git = inspect_git_provenance(tmp_path)
    assert git.inspection_error == "GIT_INSPECTION_FAILED"
    assert git.dirty_worktree is None
    assert "sensitive-test-marker" not in json.dumps(git.model_dump())
