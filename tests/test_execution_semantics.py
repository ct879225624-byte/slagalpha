"""Semantic gate tests use synthetic core artifacts and one real incomplete receipt copy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.research.execution_inputs import (
    InputArtifactRole,
    InputArtifactSelection,
    inspect_dev_execution_inputs,
)
from slagalpha.research.execution_semantics import (
    DevExecutionSemanticReport,
    inspect_dev_execution_semantics,
    write_dev_execution_semantic_report,
)
from slagalpha.research.parameters import DevParameterVersion, build_dev_parameter_version
from slagalpha.research.sensitivity import SensitivityPlan
from test_sensitivity_plan import _audit, _plan, _split


def _core_fixture(
    root: Path,
) -> tuple[SensitivityPlan, DevParameterVersion, tuple[InputArtifactSelection, ...]]:
    plan = _plan(ready=True)
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash
    )
    strategy_content = json.dumps(
        "synthetic-rules", sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    files = (
        (InputArtifactRole.STRATEGY_RULES, "inputs/strategy.md", strategy_content),
        (InputArtifactRole.ENVIRONMENT_LOCK, "inputs/requirements.lock",
         Path("requirements.lock").read_bytes()),
        (InputArtifactRole.RESEARCH_SPLIT, "inputs/split.json",
         _split().model_dump_json().encode()),
        (InputArtifactRole.RESEARCH_INPUT_AUDIT, "inputs/audit.json",
         _audit(ready=True).model_dump_json().encode()),
        (InputArtifactRole.SENSITIVITY_PLAN, "inputs/plan.json",
         plan.model_dump_json().encode()),
        (InputArtifactRole.PARAMETER_VERSION, "inputs/parameter.json",
         parameter.model_dump_json().encode()),
    )
    selections = []
    for role, relative, content in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        selections.append(InputArtifactSelection(
            role=role, relative_path=relative,
            expected_sha256=hashlib.sha256(content).hexdigest(),
        ))
    return plan, parameter, tuple(selections)


def test_valid_core_manifests_are_cross_bound_but_missing_data_stays_blocked(
    tmp_path: Path,
) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )
    semantic = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content
    )
    assert semantic.validated_roles == (
        InputArtifactRole.ENVIRONMENT_LOCK,
        InputArtifactRole.PARAMETER_VERSION,
        InputArtifactRole.RESEARCH_INPUT_AUDIT,
        InputArtifactRole.RESEARCH_SPLIT,
        InputArtifactRole.SENSITIVITY_PLAN,
        InputArtifactRole.STRATEGY_RULES,
    )
    assert semantic.status == "BLOCKED"
    assert semantic.research_authorized is False
    assert not any("SEMANTIC_" in blocker for blocker in semantic.blockers)


def test_stale_content_report_is_rejected_before_semantic_parse(tmp_path: Path) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )
    (tmp_path / "inputs" / "strategy.md").write_bytes(b"changed")
    with pytest.raises(ValueError, match="no longer matches"):
        inspect_dev_execution_semantics(
            project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content
        )


def test_incomplete_normalization_receipt_adds_semantic_blocker(tmp_path: Path) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    source = Path(
        "data/manifests/normalization_batch/"
        "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2.json"
    )
    normalization = source.read_bytes()
    relative = "inputs/normalization.json"
    (tmp_path / relative).write_bytes(normalization)
    selections = (*selections, InputArtifactSelection(
        role=InputArtifactRole.CANDLE_MULTI_TIMEFRAME,
        relative_path=relative,
        expected_sha256=hashlib.sha256(normalization).hexdigest(),
    ))
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )
    semantic = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content
    )
    assert "RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_CANDLE_MULTI_TIMEFRAME" in (
        semantic.blockers
    )
    assert InputArtifactRole.CANDLE_MULTI_TIMEFRAME in semantic.deferred_roles


def test_semantic_report_round_trip_and_tamper_rejection(tmp_path: Path) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )
    semantic = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content
    )
    path = write_dev_execution_semantic_report(semantic, tmp_path / "data")
    assert DevExecutionSemanticReport.model_validate_json(path.read_bytes()) == semantic
    assert write_dev_execution_semantic_report(semantic, tmp_path / "data") == path
    payload = semantic.model_dump(mode="json")
    payload["research_authorized"] = True
    with pytest.raises(ValidationError):
        DevExecutionSemanticReport.model_validate(payload)
