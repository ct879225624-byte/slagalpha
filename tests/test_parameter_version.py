"""Synthetic parameter-version tests; no research execution or market data is used."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.research.parameters import (
    DevParameterVersion,
    ParameterVersionError,
    build_dev_parameter_version,
    require_parameter_plan_binding,
    write_dev_parameter_version,
)
from slagalpha.research.sensitivity import build_default_sensitivity_plan
from test_sensitivity_plan import _audit, _hash, _plan, _split


def test_every_planned_candidate_gets_a_unique_content_bound_version() -> None:
    plan = _plan()
    versions = tuple(
        build_dev_parameter_version(plan=plan, candidate_hash=candidate.candidate_hash)
        for candidate in plan.candidates
    )
    assert len({version.content_hash for version in versions}) == len(plan.candidates)
    for version, candidate in zip(versions, plan.candidates, strict=True):
        assert version.candidate == candidate
        assert version.parameter_version == f"parameters/0.1.0:{version.content_hash}"
        assert version.strategy_executed is version.locked_test_consumed is False
        require_parameter_plan_binding(version, plan)


def test_blocked_plan_can_define_parameters_but_does_not_authorize_execution() -> None:
    plan = _plan(ready=False)
    version = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash
    )
    assert plan.blockers == ("NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS",)
    assert version.dataset_role == "DEV"
    assert version.strategy_executed is False


def test_unknown_candidate_and_different_plan_fail_closed() -> None:
    first = _plan()
    version = build_dev_parameter_version(
        plan=first, candidate_hash=first.candidates[0].candidate_hash
    )
    with pytest.raises(ParameterVersionError, match="not present"):
        build_dev_parameter_version(plan=first, candidate_hash="f" * 64)

    second = build_default_sensitivity_plan(
        split=_split(), audit=_audit(), strategy_rules_sha256=_hash("other rules")
    )
    with pytest.raises(ParameterVersionError, match="does not belong"):
        require_parameter_plan_binding(version, second)


def test_tampering_cannot_reuse_parameter_content_hash() -> None:
    plan = _plan()
    version = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash
    )
    payload = version.model_dump(mode="json")
    payload["sensitivity_plan_hash"] = "a" * 64
    with pytest.raises(ValidationError, match="content hash mismatch"):
        DevParameterVersion.model_validate(payload)


def test_parameter_artifact_round_trip_is_immutable(tmp_path: Path) -> None:
    plan = _plan()
    version = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash
    )
    path = write_dev_parameter_version(version, tmp_path)
    assert path.name == f"{version.content_hash}.json"
    assert DevParameterVersion.model_validate_json(path.read_bytes()) == version
    assert write_dev_parameter_version(version, tmp_path) == path
    path.write_bytes(b"changed")
    with pytest.raises(ParameterVersionError, match="changed"):
        write_dev_parameter_version(version, tmp_path)
