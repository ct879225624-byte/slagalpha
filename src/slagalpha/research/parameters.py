"""Content-addressed DEV parameter versions bound to one sensitivity-plan candidate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from slagalpha.research.sensitivity import SensitivityCandidate, SensitivityPlan
from slagalpha.research.splits import DatasetRole


class ParameterVersionError(ValueError):
    """Raised when parameter content cannot be proven to belong to its research plan."""


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class DevParameterVersion(BaseModel):
    """One immutable planned DEV configuration; never an execution authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-parameter-version/0.1.0"] = (
        "dev-parameter-version/0.1.0"
    )
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    strategy_version: str
    strategy_rules_sha256: str
    sensitivity_plan_hash: str
    candidate: SensitivityCandidate
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    content_hash: str

    @property
    def parameter_version(self) -> str:
        return f"parameters/0.1.0:{self.content_hash}"

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        for value in (
            self.strategy_rules_sha256,
            self.sensitivity_plan_hash,
            self.content_hash,
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("parameter evidence references must be lowercase SHA-256")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != _hash(payload):
            raise ValueError("parameter version content hash mismatch")
        return self


def build_dev_parameter_version(
    *, plan: SensitivityPlan, candidate_hash: str,
) -> DevParameterVersion:
    """Bind one exact candidate without relaxing the plan's current blockers."""

    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    candidate = next(
        (item for item in plan.candidates if item.candidate_hash == candidate_hash), None
    )
    if candidate is None:
        raise ParameterVersionError("candidate is not present in the sensitivity plan")
    payload = {
        "schema_version": "dev-parameter-version/0.1.0",
        "dataset_role": DatasetRole.DEV.value,
        "strategy_version": plan.strategy_version,
        "strategy_rules_sha256": plan.strategy_rules_sha256,
        "sensitivity_plan_hash": plan.plan_hash,
        "candidate": candidate.model_dump(mode="json"),
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    return DevParameterVersion.model_validate({**payload, "content_hash": _hash(payload)})


def require_parameter_plan_binding(
    version: DevParameterVersion, plan: SensitivityPlan,
) -> None:
    """Fail closed unless the parameter bytes identify an exact candidate in this plan."""

    version = DevParameterVersion.model_validate(version.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    if (
        version.strategy_version != plan.strategy_version
        or version.strategy_rules_sha256 != plan.strategy_rules_sha256
        or version.sensitivity_plan_hash != plan.plan_hash
        or version.candidate not in plan.candidates
    ):
        raise ParameterVersionError("parameter version does not belong to the sensitivity plan")


def write_dev_parameter_version(version: DevParameterVersion, data_dir: Path) -> Path:
    """Write an immutable parameter artifact indexed by its canonical content hash."""

    version = DevParameterVersion.model_validate(version.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "parameter_version" / f"{version.content_hash}.json"
    )
    content = (
        json.dumps(version.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != content:
            raise ParameterVersionError(f"existing parameter version changed: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
