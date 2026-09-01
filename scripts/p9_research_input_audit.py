"""Freeze the real P9 split and audit all Universe member-days without research runs."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.splits import (
    ResearchReadiness,
    audit_research_inputs,
    build_research_split,
    write_research_input_audit,
    write_research_split,
)

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_RUN_VERSION = "d1d2d072b00341396760f7272ac5fb534bfe15dcb9fa23d60b650c21db54b32b"
DAILY_SNAPSHOT_HASH = "2ae73f816286e7932f88d2e7c8859a3c5ad8146df4b0ae59dc242faaae1412f8"
RULE_REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"
RESEARCH_START = date(2023, 8, 1)
RESEARCH_END_EXCLUSIVE = date(2026, 8, 1)


def main() -> None:
    data_dir = ROOT / "data"
    manifests = data_dir / "manifests"
    progress_dir = manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION
    snapshots: list[UniverseSnapshot] = []
    for progress_path in sorted(progress_dir.glob("*.json")):
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        version = str(progress["universe_version"])
        snapshots.append(
            UniverseSnapshot.model_validate_json(
                (manifests / "universe_snapshot" / f"{version}.json").read_text(
                    encoding="utf-8"
                )
            )
        )
    registry = ContractRegistry.model_validate_json(
        (manifests / "contract_registry" / f"{RULE_REGISTRY_HASH}.json").read_text(
            encoding="utf-8"
        )
    )
    split = build_research_split(
        research_start=RESEARCH_START,
        research_end_exclusive=RESEARCH_END_EXCLUSIVE,
        universe_batch_run_version=UNIVERSE_RUN_VERSION,
        daily_snapshot_hash=DAILY_SNAPSHOT_HASH,
    )
    audit = audit_research_inputs(
        split=split,
        snapshots=tuple(snapshots),
        registry=registry,
    )
    if audit.overall_readiness is not ResearchReadiness.BLOCKED:
        raise RuntimeError("real P9 input audit unexpectedly allowed strategy research")
    split_path = write_research_split(split, data_dir)
    audit_path = write_research_input_audit(audit, data_dir)
    print(
        json.dumps(
            {
                "split_hash": split.split_hash,
                "audit_hash": audit.report_hash,
                "overall_readiness": audit.overall_readiness.value,
                "locked_test_consumed": audit.locked_test_consumed,
                "roles": [role.model_dump(mode="json") for role in audit.roles],
                "split_path": str(split_path.relative_to(ROOT)),
                "audit_path": str(audit_path.relative_to(ROOT)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
