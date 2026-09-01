"""Run and repeat the frozen 36-month daily historical Universe batch."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.data.universe_batch import run_universe_batch
from slagalpha.domain.universe import ContractIdentityRegistry, ExclusionLedger

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_REGISTRY_HASH = (
    "739fa834132a23afa1b15213caed637b96b6ebf863914fd6a2a69a4e94b3bd36"
)
NORMALIZATION_BATCH_HASH = (
    "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
)
SELECTION_START = date(2023, 8, 1)
SELECTION_END_EXCLUSIVE = date(2026, 8, 1)


def main() -> None:
    data_dir = ROOT / "data"
    manifests_dir = data_dir / "manifests"
    registry = ContractIdentityRegistry.model_validate_json(
        (
            manifests_dir
            / "contract_identity_registry"
            / f"{IDENTITY_REGISTRY_HASH}.json"
        ).read_text(encoding="utf-8")
    )
    normalization = NormalizationBatchResult.model_validate_json(
        (
            manifests_dir
            / "normalization_batch"
            / f"{NORMALIZATION_BATCH_HASH}.json"
        ).read_text(encoding="utf-8")
    )
    ledger = ExclusionLedger(ledger_version="empty-ledger/0.1.0", entries=())
    first = run_universe_batch(
        registry=registry,
        exclusion_ledger=ledger,
        dataset_content_hash=normalization.dataset_content_hash,
        selection_start=SELECTION_START,
        selection_end_exclusive=SELECTION_END_EXCLUSIVE,
        normalized_data_dir=data_dir / "normalized",
        manifests_dir=manifests_dir,
    )
    repeated = run_universe_batch(
        registry=registry,
        exclusion_ledger=ledger,
        dataset_content_hash=normalization.dataset_content_hash,
        selection_start=SELECTION_START,
        selection_end_exclusive=SELECTION_END_EXCLUSIVE,
        normalized_data_dir=data_dir / "normalized",
        manifests_dir=manifests_dir,
        progress_namespace="determinism-replay-2",
    )
    if (
        not first.complete
        or not repeated.complete
        or first.daily_snapshot_hash != repeated.daily_snapshot_hash
    ):
        raise RuntimeError("36-month Universe batch failed deterministic acceptance")
    print(
        json.dumps(
            {
                "run_version": first.run_version,
                "repeat_progress_version": repeated.progress_version,
                "first_result_hash": first.result_hash,
                "repeat_result_hash": repeated.result_hash,
                "daily_snapshot_hash": first.daily_snapshot_hash,
                "expected_count": first.expected_count,
                "first_computed_count": first.computed_count,
                "first_reused_count": first.reused_count,
                "repeat_computed_count": repeated.computed_count,
                "total_member_count": first.total_member_count,
                "total_blocked_count": first.total_blocked_count,
                "repeatable": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
