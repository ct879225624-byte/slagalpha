"""Semantic gate tests use synthetic core artifacts and one real incomplete receipt copy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.execution_inputs import (
    REQUIRED_INPUT_ROLES,
    DevExecutionInputReport,
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
from slagalpha.research.request_set_market_data import verify_dev_request_set_market_data
from slagalpha.research.sensitivity import SensitivityPlan
from test_dependency_artifacts import LOCK, _manifest, _wheel
from test_replay_loading import _fixture as _replay_fixture
from test_request_set_market_data import _market_set_context
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    environment = next(
        selection
        for selection in selections
        if selection.role is InputArtifactRole.ENVIRONMENT_LOCK
    )
    monkeypatch.setattr(
        "slagalpha.research.execution_semantics.inspect_environment_lock",
        lambda _: environment.expected_sha256,
    )
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


def _real_gap_selections(
    root: Path,
    *,
    overrides: dict[str, Any] | None = None,
    dev_dependency: bool = False,
) -> tuple[SensitivityPlan, DevParameterVersion, tuple[InputArtifactSelection, ...]]:
    manifests = Path("data/manifests")
    plan = SensitivityPlan.model_validate_json((manifests / "sensitivity_plan" /
        "c41e2771a8ca526e4c8ffa09a863b078e300fc7332b9090461e11e1c1f2b5ff9.json").read_bytes())
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash,
    )
    files = (
        (InputArtifactRole.RESEARCH_SPLIT, "research_split",
         "b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca"),
        (InputArtifactRole.UNIVERSE, "universe_batch",
         "1b995e73691ae8b034f429677781e727716fb2909537f1f7643f04ec5cbae520"),
        (InputArtifactRole.CANDLE_MULTI_TIMEFRAME, "normalization_batch",
         "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"),
        (InputArtifactRole.NORMALIZATION_GAP_AUDIT, "normalization_gap_audit",
         "9fdfe51b0a209451b2bae612f427ba33702b8225b71b03211372f0c986c1a7fc"),
    )
    selections = []
    for role, folder, digest in files:
        content = (manifests / folder / f"{digest}.json").read_bytes()
        if role is InputArtifactRole.NORMALIZATION_GAP_AUDIT and (overrides or dev_dependency):
            payload = json.loads(content)
            payload.update(overrides or {})
            if dev_dependency:
                dependency = payload["dependencies"][0]
                dependency["recursive_history_overlap_dates"] = ["2024-01-01"]
                payload["recursive_history_overlap_day_count"] += 1
                payload["blockers"].append(
                    "RECURSIVE_HISTORY_DEPENDENCY_UNRESOLVED:"
                    + dependency["evidence"]["identity"]
                )
                payload["blockers"] = sorted(set(payload["blockers"]))
            del payload["report_hash"]
            payload["report_hash"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
            content = canonical_json_bytes(payload)
        relative = f"{folder}.json"
        (root / relative).write_bytes(content)
        selections.append(InputArtifactSelection(
            role=role, relative_path=relative,
            expected_sha256=hashlib.sha256(content).hexdigest(),
        ))
    return plan, parameter, tuple(selections)


def test_bound_gap_diagnostics_clear_only_out_of_scope_dev_failures(tmp_path: Path) -> None:
    plan, parameter, selections = _real_gap_selections(tmp_path)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections,
    )
    report = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content,
    )
    assert report.schema_version == "dev-execution-semantics/0.3.0"
    assert report.normalization_scope is not None
    assert report.normalization_scope.failed_partition_count == 27
    assert report.normalization_scope.in_scope_failure_count == 0
    assert not any(code.startswith("RUN_INPUT_GAP_AUDIT:") for code in report.blockers)
    assert "RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_CANDLE_MULTI_TIMEFRAME" not in (
        report.blockers
    )
    assert InputArtifactRole.CANDLE_MULTI_TIMEFRAME in report.validated_roles
    assert report.research_authorized is False
    assert InputArtifactRole.NORMALIZATION_GAP_AUDIT not in REQUIRED_INPUT_ROLES


def test_dev_dependency_keeps_incomplete_normalization_blocked(tmp_path: Path) -> None:
    plan, parameter, selections = _real_gap_selections(tmp_path, dev_dependency=True)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections,
    )
    report = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content,
    )
    assert report.normalization_scope is None
    assert report.schema_version == "dev-execution-semantics/0.1.0"
    assert any(code.startswith("RUN_INPUT_GAP_AUDIT:") for code in report.blockers)
    assert "RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_CANDLE_MULTI_TIMEFRAME" in (
        report.blockers
    )
    assert InputArtifactRole.CANDLE_MULTI_TIMEFRAME in report.deferred_roles


@pytest.mark.parametrize("overrides", [
    {"normalization_result_hash": "a" * 64},
    {"daily_snapshot_hash": "a" * 64},
    {"snapshot_count": 1},
    {"finite_lookback_bars": 1},
])
def test_gap_diagnostics_from_other_inputs_cannot_be_cross_bound(
    tmp_path: Path, overrides: dict[str, Any],
) -> None:
    plan, parameter, selections = _real_gap_selections(tmp_path, overrides=overrides)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections,
    )
    report = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content,
    )
    assert "RUN_INPUT_SEMANTIC_MISMATCH_NORMALIZATION_GAP_AUDIT" in report.blockers


def test_saved_reports_without_optional_gap_diagnostics_remain_valid() -> None:
    manifests = Path("data/manifests")
    content = DevExecutionInputReport.model_validate_json((manifests / "dev_execution_inputs" /
        "c3a6db4305badba848fb9b804f662052f3af7cd002e3f3270e10899354d026c0.json").read_bytes())
    semantic = DevExecutionSemanticReport.model_validate_json((
        manifests / "dev_execution_semantics" /
        "00104d25f2d297d2160a0ee471c1ac29fab4713744bdd8582e0b80a82e5a23ad.json").read_bytes())
    assert content.status == semantic.status == "BLOCKED"


@pytest.mark.parametrize("tamper", [False, True])
def test_dependency_gate_rehashes_wheels_not_just_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: bool,
) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    _wheel(tmp_path)
    manifest = _manifest(tmp_path)
    relative = "inputs/dependencies.json"
    content_bytes = manifest.model_dump_json().encode()
    (tmp_path / relative).write_bytes(content_bytes)
    lock_relative = "inputs/requirements.lock"
    (tmp_path / lock_relative).write_bytes(LOCK)
    selections = tuple(item for item in selections
                       if item.role is not InputArtifactRole.ENVIRONMENT_LOCK)
    selections = (*selections, InputArtifactSelection(
        role=InputArtifactRole.ENVIRONMENT_LOCK, relative_path=lock_relative,
        expected_sha256=hashlib.sha256(LOCK).hexdigest(),
    ), InputArtifactSelection(
        role=InputArtifactRole.DEPENDENCY_ARTIFACTS, relative_path=relative,
        expected_sha256=hashlib.sha256(content_bytes).hexdigest(),
    ))
    # Only installed-version inspection is mocked; wheel metadata/hash checks are real.
    monkeypatch.setattr(
        "slagalpha.research.execution_semantics.inspect_environment_lock",
        lambda _: hashlib.sha256(LOCK).hexdigest(),
    )
    if tamper:
        (tmp_path / "wheels" / manifest.wheels[0].filename).write_bytes(b"corrupted wheel")
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections,
    )
    report = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content,
    )
    assert (InputArtifactRole.DEPENDENCY_ARTIFACTS in report.validated_roles) is not tamper
    assert ("RUN_INPUT_SEMANTIC_INVALID_DEPENDENCY_ARTIFACTS" in report.blockers) is tamper


@pytest.mark.parametrize("mismatched_role", [False, True])
def test_single_replay_artifact_cannot_claim_full_dev_coverage(
    tmp_path: Path, mismatched_role: bool,
) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    _, _, one_minute, funding = _replay_fixture(tmp_path)
    artifact = funding if mismatched_role else one_minute
    content_bytes = artifact.model_dump_json().encode()
    relative = "inputs/single-replay.json"
    (tmp_path / relative).write_bytes(content_bytes)
    selections = (*selections, InputArtifactSelection(
        role=InputArtifactRole.CANDLE_ONE_MINUTE, relative_path=relative,
        expected_sha256=hashlib.sha256(content_bytes).hexdigest(),
    ))
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections,
    )
    report = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content,
    )
    reason = "MISMATCH" if mismatched_role else "REQUEST_SET_COVERAGE_REQUIRED"
    assert f"RUN_INPUT_SEMANTIC_{reason}_CANDLE_ONE_MINUTE" in report.blockers
    assert InputArtifactRole.CANDLE_ONE_MINUTE in report.deferred_roles
    assert report.research_authorized is False


@pytest.mark.parametrize("role", [InputArtifactRole.CANDLE_ONE_MINUTE, InputArtifactRole.FUNDING])
@pytest.mark.parametrize("case", ["valid", "missing_raw", "invalid_hash", "forged_authority"])
def test_aggregate_receipt_never_replaces_live_source_revalidation(
    tmp_path: Path, role: InputArtifactRole, case: str,
) -> None:
    context = _market_set_context(tmp_path)
    receipt = verify_dev_request_set_market_data(**context)
    plan, parameter, selections = _core_fixture(tmp_path)
    payload = receipt.model_dump(mode="json")
    if case == "missing_raw":
        (tmp_path / "minutes-1.json").unlink()
    elif case == "invalid_hash":
        payload["report_hash"] = "0" * 64
    elif case == "forged_authority":
        payload["research_authorized"] = True
        payload.pop("report_hash")
        payload["report_hash"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    content_bytes = canonical_json_bytes(payload)
    relative = "inputs/aggregate-replay.json"
    (tmp_path / relative).write_bytes(content_bytes)
    selections = (*selections, InputArtifactSelection(
        role=role, relative_path=relative,
        expected_sha256=hashlib.sha256(content_bytes).hexdigest(),
    ))
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections,
    )
    report = inspect_dev_execution_semantics(
        project_dir=tmp_path, plan=plan, parameter=parameter, content_report=content,
    )
    reason = ("REQUEST_SET_SOURCE_REVALIDATION_REQUIRED" if case in ("valid", "missing_raw")
              else "INVALID")
    assert f"RUN_INPUT_SEMANTIC_{reason}_{role.value}" in report.blockers
    assert role in report.deferred_roles
    assert role not in report.validated_roles
    assert report.status == "BLOCKED"
    assert report.research_authorized is False
