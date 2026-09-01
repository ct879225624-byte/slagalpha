"""Evaluate the real P8 Universe against the fail-closed contract-rule draft."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.backtest.historical import (
    evaluate_universe_rule_gate,
    write_historical_rule_gate,
)
from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_HASH = "67539b969558882f19eda7aaa83b08054a498efbb8ae28d0ad4dbbcd824f31c5"
RULE_REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"


def main() -> None:
    data_dir = ROOT / "data"
    manifests = data_dir / "manifests"
    universe = UniverseSnapshot.model_validate_json(
        (manifests / "universe_snapshot" / f"{UNIVERSE_HASH}.json").read_text(
            encoding="utf-8"
        )
    )
    registry = ContractRegistry.model_validate_json(
        (manifests / "contract_registry" / f"{RULE_REGISTRY_HASH}.json").read_text(
            encoding="utf-8"
        )
    )
    first = evaluate_universe_rule_gate(universe, registry)
    repeated = evaluate_universe_rule_gate(universe, registry)
    if first != repeated or first.eligible_count != 0:
        raise RuntimeError("real historical rule gate did not fail closed deterministically")
    output = write_historical_rule_gate(first, data_dir)
    print(
        json.dumps(
            {
                "universe_version": first.universe_version,
                "contract_registry_version": first.contract_registry_version,
                "member_count": first.member_count,
                "eligible_count": first.eligible_count,
                "blocked_count": first.blocked_count,
                "reason_counts": first.reason_counts,
                "report_hash": first.report_hash,
                "report_path": str(output.relative_to(ROOT)),
                "repeatable": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
