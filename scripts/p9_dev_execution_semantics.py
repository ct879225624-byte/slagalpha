"""Parse and cross-check the content-verified DEV manifests without running research."""

from __future__ import annotations

import json

from p9_dev_execution_inputs import PARAMETER_HASH, PLAN_HASH, ROOT, SELECTIONS

from slagalpha.research.execution_inputs import (
    InputArtifactSelection,
    inspect_dev_execution_inputs,
)
from slagalpha.research.execution_semantics import (
    inspect_dev_execution_semantics,
    write_dev_execution_semantic_report,
)
from slagalpha.research.lifecycle_replacement import (
    LifecycleReplacementNormalizationResult,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.sensitivity import SensitivityPlan

REPLACEMENT_HASH = "7565da18af89195250651ba2cf49f60ae4f8725ce9a83a2b010e540e5047911b"


def main() -> int:
    manifests = ROOT / "data" / "manifests"
    plan = SensitivityPlan.model_validate_json(
        (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    parameter = DevParameterVersion.model_validate_json(
        (manifests / "parameter_version" / f"{PARAMETER_HASH}.json").read_bytes()
    )
    selections = tuple(
        InputArtifactSelection(role=role, relative_path=path, expected_sha256=digest)
        for role, path, digest in SELECTIONS
    )
    content = inspect_dev_execution_inputs(
        project_dir=ROOT, plan=plan, parameter=parameter, selections=selections
    )
    replacement = LifecycleReplacementNormalizationResult.model_validate_json(
        (
            manifests / "lifecycle_replacement_normalization" / f"{REPLACEMENT_HASH}.json"
        ).read_bytes()
    )
    report = inspect_dev_execution_semantics(
        project_dir=ROOT,
        plan=plan,
        parameter=parameter,
        content_report=content,
        replacement=replacement,
        expected_replacement_hash=REPLACEMENT_HASH,
    )
    path = write_dev_execution_semantic_report(report, ROOT / "data")
    print(
        json.dumps(
            {
                **report.model_dump(mode="json"),
                "report_path": str(path.relative_to(ROOT)),
            },
            sort_keys=True,
        )
    )
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
