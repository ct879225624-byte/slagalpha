"""Build only DEV scan obligations from frozen metadata; never evaluate a strategy."""

from __future__ import annotations

import json

from p9_dev_execution_inputs import PARAMETER_HASH, PLAN_HASH, ROOT
from p9_normalization_gap_audit import UNIVERSE_RUN_VERSION

from slagalpha.domain.universe import UniverseSnapshot
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.scan_plan import build_dev_scan_plan, write_dev_scan_plan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest


def main() -> int:
    manifests = ROOT / "data" / "manifests"
    plan = SensitivityPlan.model_validate_json(
        (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    parameter = DevParameterVersion.model_validate_json(
        (manifests / "parameter_version" / f"{PARAMETER_HASH}.json").read_bytes()
    )
    split = ResearchSplitManifest.model_validate_json(
        (manifests / "research_split" / f"{plan.split_hash}.json").read_bytes()
    )
    snapshots = []
    progress_dir = manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION
    for progress_path in sorted(progress_dir.glob("*.json")):
        progress = json.loads(progress_path.read_bytes())
        snapshots.append(UniverseSnapshot.model_validate_json(
            (manifests / "universe_snapshot" / f"{progress['universe_version']}.json").read_bytes()
        ))
    result = build_dev_scan_plan(
        split=split, plan=plan, parameter=parameter, snapshots=tuple(snapshots),
    )
    path = write_dev_scan_plan(result, ROOT / "data")
    print(json.dumps({
        "plan_hash": result.plan_hash, "day_count": len(result.days),
        "expected_record_count": result.expected_record_count,
        "upstream_blockers": result.upstream_blockers, "scan_executed": False,
        "research_authorized": False, "plan_path": str(path.relative_to(ROOT)),
    }, sort_keys=True))
    return 1 if result.upstream_blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
