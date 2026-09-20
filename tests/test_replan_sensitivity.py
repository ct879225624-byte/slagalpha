from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from slagalpha.research import replan_sensitivity as replan
from slagalpha.research.replan_sensitivity import (
    BASELINE_CANDIDATE_HASH,
    FINAL_LEDGER_HASH,
    ReplanCandidateArtifact,
    ReplanExecutorError,
    execute_replan_candidate,
    load_final_ledger,
)
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.strategy.plans import build_entry_stop

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
BASELINE_PARAMETER_HASH = "81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b"


@lru_cache(maxsize=1)
def _context() -> tuple[Any, ...]:
    plan = SensitivityPlan.model_validate_json(
        (DATA / "manifests" / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    ledger = load_final_ledger(
        DATA / "manifests" / "confirmed_trigger_ledger" / f"{FINAL_LEDGER_HASH}.json"
    )
    parameter = replan._load_parameter(
        DATA / "manifests" / "parameter_version" / f"{BASELINE_PARAMETER_HASH}.json",
        plan=plan,
        candidate_hash=BASELINE_CANDIDATE_HASH,
        expected_content_hash=BASELINE_PARAMETER_HASH,
    )
    split = replan._load_split(DATA, plan)
    registry = replan._load_registry(DATA, ledger.registry_content_hash)
    universes = replan._load_universes(DATA)
    return ledger, plan, parameter, split, registry, universes


def _run_baseline() -> ReplanCandidateArtifact:
    ledger, plan, parameter, split, registry, universes = _context()
    return execute_replan_candidate(
        candidate_hash=BASELINE_CANDIDATE_HASH,
        ledger=ledger,
        plan=plan,
        parameter=parameter,
        split=split,
        registry=registry,
        universes=universes,
    )


@pytest.fixture(scope="module")
def baseline_artifact() -> ReplanCandidateArtifact:
    return _run_baseline()


def test_baseline_equivalence_against_trusted_ledger(
    baseline_artifact: ReplanCandidateArtifact,
) -> None:
    artifact = baseline_artifact
    assert artifact.input_record_count == 2946
    assert artifact.p6_status_counts == {
        "REJECTED_ENTRY_STOP": 2294,
        "REJECTED_TAKE_PROFIT": 625,
        "REJECTED_REQUEST_BOUNDARY": 0,
        "ACCEPTED_PLAN": 27,
    }
    trusted = {
        item.logical_signal_id
        for item in _context()[0].records
        if item.baseline_p6_status == "ACCEPTED_PLAN"
    }
    assert set(artifact.accepted_logical_signal_ids) == trusted


def test_deterministic_rerun_has_identical_content_hash(
    baseline_artifact: ReplanCandidateArtifact,
) -> None:
    assert _run_baseline().artifact_hash == baseline_artifact.artifact_hash


def test_executor_does_not_use_baseline_requests_as_input(
    monkeypatch: pytest.MonkeyPatch, baseline_artifact: ReplanCandidateArtifact,
) -> None:
    calls = 0
    original = build_entry_stop

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("slagalpha.research.replan_sensitivity.build_entry_stop", counted)
    artifact = _run_baseline()
    assert calls == 2946
    assert artifact.input_record_count == 2946
    assert artifact.accepted_count == 27


def test_malformed_ledger_context_fails_closed() -> None:
    ledger, plan, parameter, split, registry, universes = _context()
    record = ledger.records[0].model_copy(update={"visible_hourly_zones": None})
    malformed = ledger.model_copy(update={"records": (record, *ledger.records[1:])})
    with pytest.raises(ReplanExecutorError):
        execute_replan_candidate(
            candidate_hash=BASELINE_CANDIDATE_HASH,
            ledger=malformed,
            plan=plan,
            parameter=parameter,
            split=split,
            registry=registry,
            universes=universes,
        )


def test_candidate_parameter_binding_fails_closed(
    baseline_artifact: ReplanCandidateArtifact,
) -> None:
    ledger, plan, parameter, split, registry, universes = _context()
    with pytest.raises(ReplanExecutorError):
        execute_replan_candidate(
            candidate_hash="bd7578e397a1a0d72c52a4d0244fa9dd2d73792f07b8181d886fefe695060d38",
            ledger=ledger,
            plan=plan,
            parameter=parameter,
            split=split,
            registry=registry,
            universes=universes,
        )
