"""Run one explicit Phase 3 Stage A candidate; no download or replay."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.dev_source_scan import ConfirmedTriggerLedger, DevSourceScanReport
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.rescan_stage_a import (
    BASELINE_REPORT_HASH,
    FINAL_LEDGER_HASH,
    StageAFunnelArtifact,
    execute_stage_a,
)
from slagalpha.research.scan_plan import DevScanPlan, build_dev_scan_plan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
SCAN_PLAN_HASH = "02c709fa3dfdce157003e5695ba29eda3f446b25f125fca08ebea73d5e5fabb4"
REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"
UNIVERSE_RUN_VERSION = "d1d2d072b00341396760f7272ac5fb534bfe15dcb9fa23d60b650c21db54b32b"
SUPPORTED = {
    "e7af960eec91b8e8b8abf5484f3ef98aab73a90660121714746c3b24408d49c5":
        "81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b",
    "d889a882a5f551931d741752b28b65e1779cc3d247729abff249a221d6b976c4":
        "5bf9601a38d82463cdb33b8fd4c69a019c998bee116ffa90e14528ee22a49956",
    "def218e7a145e94ae8c08f2a601f337e1f53f871e7bda96339514af9f8c3a706":
        "24f57efe6396cef12ff70b7d6e9cea235e56c3485055acb21037ead0ede04bb4",
    "15ca63f9a27f254d6cc0117b586e58399d500893ccd263ac6729cc09752f064f":
        "7fa372f8cd3b7c7ee5ed68a14d4fa4291651c526bccbba9f843c4b0cde2c4859",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 Stage A candidate-bound scanner")
    parser.add_argument("--candidate-hash", required=True, choices=tuple(SUPPORTED))
    parser.add_argument("--run", action="store_true", help="execute the selected candidate")
    parser.add_argument(
        "--baseline-gate", type=Path,
        help="required immutable baseline gate artifact for a non-baseline candidate",
    )
    args = parser.parse_args(argv)
    manifests = ROOT / "data" / "manifests"
    plan = SensitivityPlan.model_validate_json(
        (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    parameter = DevParameterVersion.model_validate_json(
        (manifests / "parameter_version" / f"{SUPPORTED[args.candidate_hash]}.json").read_bytes()
    )
    trusted = DevSourceScanReport.model_validate_json(
        (manifests / "dev_source_scan" / f"{BASELINE_REPORT_HASH}.json").read_bytes()
    )
    final_ledger = ConfirmedTriggerLedger.model_validate_json(
        (manifests / "confirmed_trigger_ledger" / f"{FINAL_LEDGER_HASH}.json").read_bytes()
    )
    split = ResearchSplitManifest.model_validate_json(
        (manifests / "research_split" / f"{plan.split_hash}.json").read_bytes()
    )
    scan_plan = DevScanPlan.model_validate_json(
        (manifests / "dev_scan_plan" / f"{SCAN_PLAN_HASH}.json").read_bytes()
    )
    registry = ContractRegistry.model_validate_json(
        (manifests / "contract_registry" / f"{REGISTRY_HASH}.json").read_bytes()
    )
    snapshots = []
    progress_dir = manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION
    for path in sorted(progress_dir.glob("*.json")):
        progress = json.loads(path.read_bytes())
        snapshots.append(UniverseSnapshot.model_validate_json(
            (manifests / "universe_snapshot" / f"{progress['universe_version']}.json").read_bytes()
        ))
    if parameter.candidate.changed_parameter != "BASELINE":
        scan_plan = build_dev_scan_plan(
            split=split, plan=plan, parameter=parameter, snapshots=tuple(snapshots)
        )
    if not args.run:
        print(json.dumps({"status": "READY", "candidate_hash": args.candidate_hash,
                          "run_started": False, "download_started": False,
                          "replay_executed": False}, sort_keys=True))
        return 0
    baseline_gate = None
    if parameter.candidate.changed_parameter != "BASELINE":
        if args.baseline_gate is None:
            parser.error("--baseline-gate is required for a non-baseline candidate")
        baseline_gate = StageAFunnelArtifact.model_validate_json(
            args.baseline_gate.read_bytes()
        )
    lock_hash = hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest()
    artifact, path = execute_stage_a(
        project_dir=ROOT, plan=plan, parameter=parameter, split=split,
        scan_plan=scan_plan, registry=registry, snapshots=tuple(snapshots),
        checkpoint_dir=ROOT / "data" / "checkpoints" / "p9_stage_a"
        / args.candidate_hash, environment_lock_hash=lock_hash,
        trusted_report=trusted, final_ledger=final_ledger,
        baseline_gate=baseline_gate,
    )
    print(artifact.model_dump_json(indent=2))
    print(f"artifact_path={path}")
    return 0 if artifact.status == "COMPLETE" and artifact.phase_3_gate != "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
