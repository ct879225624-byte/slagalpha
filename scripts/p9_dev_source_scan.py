"""Run the frozen baseline P3-P6 DEV scan; never download replay inputs."""

from __future__ import annotations

import json

from p9_dev_execution_inputs import PARAMETER_HASH, PLAN_HASH, ROOT
from p9_normalization_gap_audit import UNIVERSE_RUN_VERSION

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.dev_source_scan import (
    build_dev_source_scan_report,
    write_dev_source_scan_report,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest

SCAN_PLAN_HASH = "02c709fa3dfdce157003e5695ba29eda3f446b25f125fca08ebea73d5e5fabb4"
REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"


def main() -> int:
    manifests = ROOT / "data/manifests"
    plan = SensitivityPlan.model_validate_json(
        (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    parameter = DevParameterVersion.model_validate_json(
        (manifests / "parameter_version" / f"{PARAMETER_HASH}.json").read_bytes()
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
    for progress_path in sorted(
        (manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION).glob("*.json")
    ):
        progress_payload = json.loads(progress_path.read_bytes())
        snapshots.append(UniverseSnapshot.model_validate_json(
            (
                manifests
                / "universe_snapshot"
                / f"{progress_payload['universe_version']}.json"
            ).read_bytes()
        ))

    def progress(position: int, total: int, symbol: str, slots: int, accepted: int) -> None:
        print(json.dumps({
            "symbols_completed": position,
            "symbols_total": total,
            "symbol": symbol,
            "symbol_slots": slots,
            "symbol_accepted": accepted,
        }, sort_keys=True), flush=True)

    report = build_dev_source_scan_report(
        project_dir=ROOT,
        scan_plan=scan_plan,
        split=split,
        plan=plan,
        parameter=parameter,
        snapshots=tuple(snapshots),
        registry=registry,
        checkpoint_dir=ROOT / "data/checkpoints/dev_source_scan",
        progress=progress,
    )
    path = write_dev_source_scan_report(report, ROOT / "data")
    print(json.dumps({
        "status": report.status,
        "report_hash": report.report_hash,
        "request_set_hash": report.request_set_hash,
        "funnel": report.funnel.model_dump(mode="json"),
        "market_data_requirement": report.market_data_requirement.model_dump(mode="json"),
        "anomaly_count": len(report.anomalies),
        "download_started": False,
        "replay_executed": False,
        "report_path": str(path.relative_to(ROOT)),
    }, sort_keys=True))
    return 1 if report.status == "PARTIAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
