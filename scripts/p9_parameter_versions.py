"""Materialize the ten planned DEV parameter versions without running research."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.research.parameters import (
    build_dev_parameter_version,
    require_parameter_plan_binding,
    write_dev_parameter_version,
)
from slagalpha.research.sensitivity import SensitivityPlan

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"


def main() -> None:
    plan_path = ROOT / "data" / "manifests" / "sensitivity_plan" / f"{PLAN_HASH}.json"
    plan = SensitivityPlan.model_validate_json(plan_path.read_bytes())
    if plan.plan_hash != PLAN_HASH:
        raise ValueError("sensitivity plan identity does not match its selected filename")
    versions = tuple(
        build_dev_parameter_version(plan=plan, candidate_hash=candidate.candidate_hash)
        for candidate in plan.candidates
    )
    paths = []
    for version in versions:
        require_parameter_plan_binding(version, plan)
        paths.append(write_dev_parameter_version(version, ROOT / "data"))
    print(json.dumps({
        "dataset_role": "DEV",
        "sensitivity_plan_hash": plan.plan_hash,
        "parameter_version_count": len(versions),
        "parameter_versions": [version.parameter_version for version in versions],
        "paths": [str(path.relative_to(ROOT)) for path in paths],
        "plan_blockers_preserved": list(plan.blockers),
        "strategy_executed": False,
        "locked_test_consumed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
