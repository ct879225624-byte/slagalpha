"""Input-content gate tests use only temporary synthetic files and frozen planning fixtures."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.research.execution_inputs import (
    REQUIRED_INPUT_ROLES,
    DevExecutionInputReport,
    InputArtifactRole,
    InputArtifactSelection,
    inspect_dev_execution_inputs,
    write_dev_execution_input_report,
)
from slagalpha.research.parameters import build_dev_parameter_version
from test_sensitivity_plan import _plan


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parameter(*, ready: bool = True) -> tuple[Any, Any]:
    plan = _plan(ready=ready)
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash
    )
    return plan, parameter


def _complete_selections(root: Path) -> tuple[InputArtifactSelection, ...]:
    selections = []
    for role in REQUIRED_INPUT_ROLES:
        relative = f"inputs/{role.value.lower()}.bin"
        content = f"synthetic:{role.value}".encode()
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        selections.append(InputArtifactSelection(
            role=role, relative_path=relative, expected_sha256=_sha(content)
        ))
    return tuple(selections)


def test_complete_verified_content_still_does_not_authorize_research(tmp_path: Path) -> None:
    plan, parameter = _parameter()
    report = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter,
        selections=_complete_selections(tmp_path),
    )
    assert report.status == "CHECKS_PASSED"
    assert report.blockers == report.missing_roles == ()
    assert len(report.artifacts) == len(REQUIRED_INPUT_ROLES)
    assert report.strategy_executed is report.locked_test_consumed is False
    assert report.research_authorized is False
    assert report == inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter,
        selections=_complete_selections(tmp_path),
    )


def test_plan_blocker_survives_even_when_every_file_hash_matches(tmp_path: Path) -> None:
    plan, parameter = _parameter(ready=False)
    report = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter,
        selections=_complete_selections(tmp_path),
    )
    assert report.status == "BLOCKED"
    assert report.blockers == ("NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS",)
    assert report.missing_roles == ()


def test_missing_mismatched_and_unavailable_inputs_are_all_reported(tmp_path: Path) -> None:
    plan, parameter = _parameter()
    content = b"actual"
    (tmp_path / "mismatch.bin").write_bytes(content)
    selections = (
        InputArtifactSelection(
            role=InputArtifactRole.STRATEGY_RULES,
            relative_path="mismatch.bin", expected_sha256=_sha(b"expected"),
        ),
        InputArtifactSelection(
            role=InputArtifactRole.ENVIRONMENT_LOCK,
            relative_path="missing.bin", expected_sha256=_sha(b"missing"),
        ),
    )
    report = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )
    assert "RUN_INPUT_HASH_MISMATCH_STRATEGY_RULES" in report.blockers
    assert "RUN_INPUT_UNAVAILABLE_ENVIRONMENT_LOCK" in report.blockers
    assert InputArtifactRole.STRATEGY_RULES in report.missing_roles
    assert InputArtifactRole.ENVIRONMENT_LOCK in report.missing_roles
    assert report.artifacts == ()


@pytest.mark.parametrize("relative_path", [
    "../secret", "/absolute", "a\\b", "./input", "a//b", "C:/input",
])
def test_noncanonical_or_escaping_paths_are_rejected(relative_path: str) -> None:
    with pytest.raises(ValidationError, match="canonical project-relative"):
        InputArtifactSelection(
            role=InputArtifactRole.STRATEGY_RULES,
            relative_path=relative_path, expected_sha256="a" * 64,
        )


def test_symlink_input_is_blocked_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"target")
    link = tmp_path / "link.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    plan, parameter = _parameter()
    report = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter,
        selections=(InputArtifactSelection(
            role=InputArtifactRole.STRATEGY_RULES,
            relative_path="link.bin", expected_sha256=_sha(b"target"),
        ),),
    )
    assert "RUN_INPUT_UNSAFE_PATH_STRATEGY_RULES" in report.blockers


def test_report_tampering_and_duplicate_selection_fail_closed(tmp_path: Path) -> None:
    plan, parameter = _parameter()
    selections = _complete_selections(tmp_path)
    report = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )
    payload = report.model_dump(mode="json")
    payload["research_authorized"] = True
    with pytest.raises(ValidationError):
        DevExecutionInputReport.model_validate(payload)
    with pytest.raises(ValueError, match="duplicate"):
        inspect_dev_execution_inputs(
            project_dir=tmp_path, plan=plan, parameter=parameter,
            selections=(selections[0], selections[0]),
        )


def test_report_round_trip_is_immutable(tmp_path: Path) -> None:
    plan, parameter = _parameter()
    report = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter,
        selections=_complete_selections(tmp_path),
    )
    data_dir = tmp_path / "data"
    path = write_dev_execution_input_report(report, data_dir)
    assert DevExecutionInputReport.model_validate_json(path.read_bytes()) == report
    assert write_dev_execution_input_report(report, data_dir) == path
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        write_dev_execution_input_report(report, data_dir)
