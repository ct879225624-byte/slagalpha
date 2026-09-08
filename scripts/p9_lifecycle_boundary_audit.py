"""Audit known relist boundaries without changing normalization or research gates."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.domain.universe import ContractIdentityRegistry
from slagalpha.research.lifecycle_boundaries import (
    build_lifecycle_boundary_audit,
    write_lifecycle_boundary_audit,
)

ROOT = Path(__file__).resolve().parents[1]
NORMALIZATION_HASH = "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
IDENTITY_REGISTRY_HASH = "739fa834132a23afa1b15213caed637b96b6ebf863914fd6a2a69a4e94b3bd36"
TARGET_SYMBOLS = (
    "AERGOUSDT",
    "AIAUSDT",
    "CTKUSDT",
    "CVCUSDT",
    "CVXUSDT",
    "LITUSDT",
    "MAVIAUSDT",
    "PUMPUSDT",
    "SLPUSDT",
)


def main() -> int:
    data_dir = ROOT / "data"
    manifests = data_dir / "manifests"
    normalization = NormalizationBatchResult.model_validate_json(
        (manifests / "normalization_batch" / f"{NORMALIZATION_HASH}.json").read_bytes()
    )
    identity_registry = ContractIdentityRegistry.model_validate_json(
        (
            manifests
            / "contract_identity_registry"
            / f"{IDENTITY_REGISTRY_HASH}.json"
        ).read_bytes()
    )
    report = build_lifecycle_boundary_audit(
        normalization=normalization,
        identity_registry=identity_registry,
        target_symbols=TARGET_SYMBOLS,
        monthly_klines_dir=(
            data_dir / "raw" / "binance" / "futures" / "um" / "monthly" / "klines"
        ),
    )
    path = write_lifecycle_boundary_audit(report, data_dir)
    print(
        json.dumps(
            {
                "status": report.status,
                "report_hash": report.report_hash,
                "target_count": report.target_count,
                "confirmed_relist_count": report.confirmed_relist_count,
                "unresolved_count": report.unresolved_count,
                "daily_mixed_lifecycle_count": report.daily_mixed_lifecycle_count,
                "symbol_statuses": {
                    item.symbol: item.status for item in report.symbols
                },
                "blockers": list(report.blockers),
                "normalization_result_mutation_authorized": False,
                "atr_reset_authorized": False,
                "history_seed_authorized": False,
                "historical_rule_gate_relaxation_authorized": False,
                "research_authorized": False,
                "strategy_executed": False,
                "locked_test_consumed": False,
                "report_path": str(path.relative_to(ROOT)),
            },
            sort_keys=True,
        )
    )
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
