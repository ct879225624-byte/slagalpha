"""Create a DEV evidence collection queue from real local Universe metadata only."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.rule_gaps import build_dev_rule_gap_report, write_dev_rule_gap_report
from slagalpha.research.splits import ResearchSplitManifest

ROOT = Path(__file__).resolve().parents[1]
SPLIT_HASH = "b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca"
RULE_REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"


def main() -> None:
    manifests = ROOT / "data" / "manifests"
    split = ResearchSplitManifest.model_validate_json(
        (manifests / "research_split" / f"{SPLIT_HASH}.json").read_text(encoding="utf-8")
    )
    progress_dir = manifests / "universe_batch_progress" / split.universe_batch_run_version
    snapshots = []
    for path in sorted(progress_dir.glob("*.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        version = receipt["universe_version"]
        snapshots.append(UniverseSnapshot.model_validate_json(
            (manifests / "universe_snapshot" / f"{version}.json").read_text(encoding="utf-8")
        ))
    registry = ContractRegistry.model_validate_json(
        (manifests / "contract_registry" / f"{RULE_REGISTRY_HASH}.json").read_text(encoding="utf-8")
    )
    report = build_dev_rule_gap_report(split=split, snapshots=tuple(snapshots), registry=registry)
    path = write_dev_rule_gap_report(report, ROOT / "data")
    print(json.dumps({
        "report_hash": report.report_hash,
        "dataset_role": report.dataset_role.value,
        "coverage_scope": report.coverage_scope,
        "blocked_member_day_count": report.blocked_member_day_count,
        "eligible_member_day_count": report.eligible_member_day_count,
        "symbol_count": len(report.targets),
        "gap_window_count": sum(len(target.windows) for target in report.targets),
        "first_ten_targets": [
            {"symbol": t.symbol, "blocked_member_day_count": t.blocked_member_day_count}
            for t in report.targets[:10]
        ],
        "report_path": str(path.relative_to(ROOT)),
        "strategy_executed": False,
        "locked_test_consumed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
