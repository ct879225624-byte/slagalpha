"""Persist the default DEV study plan and demonstrate the real-input execution block."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from slagalpha.research.sensitivity import (
    SensitivityPlanError,
    build_default_sensitivity_plan,
    require_dev_execution_inputs,
    write_sensitivity_plan,
)
from slagalpha.research.splits import ResearchInputAuditReport, ResearchSplitManifest

ROOT = Path(__file__).resolve().parents[1]
SPLIT_HASH = "b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca"
AUDIT_HASH = "d58f1cc2adcad91ebbc33bc3864e1f78388b2c9c8f6adb83f1b88f5b3d99e864"


def main() -> None:
    manifests = ROOT / "data" / "manifests"
    split = ResearchSplitManifest.model_validate_json(
        (manifests / "research_split" / f"{SPLIT_HASH}.json").read_text(encoding="utf-8")
    )
    audit = ResearchInputAuditReport.model_validate_json(
        (manifests / "research_input_audit" / f"{AUDIT_HASH}.json").read_text(encoding="utf-8")
    )
    rules_hash = hashlib.sha256((ROOT / "docs" / "strategy-rules-v0.1.md").read_bytes()).hexdigest()
    plan = build_default_sensitivity_plan(
        split=split, audit=audit, strategy_rules_sha256=rules_hash
    )
    path = write_sensitivity_plan(plan, ROOT / "data")
    try:
        require_dev_execution_inputs(plan, audit)
    except SensitivityPlanError as error:
        gate_result = str(error)
    else:
        gate_result = "DEV prerequisite passed; no strategy execution requested by this script"
    print(json.dumps({
        "plan_hash": plan.plan_hash,
        "candidate_count": len(plan.candidates),
        "planned_evaluation_count": plan.planned_evaluation_count,
        "dataset_role": plan.dataset_role.value,
        "gate_result": gate_result,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "plan_path": str(path.relative_to(ROOT)),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
