"""Run one real-data P8 Universe snapshot twice for acceptance evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from slagalpha.data.klines import ArchiveNormalizationManifest
from slagalpha.data.universe import (
    REQUIRED_INTERVALS,
    build_daily_universe,
    write_universe_snapshot,
)
from slagalpha.domain.universe import ContractIdentityRegistry, ExclusionLedger

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_HASH = "739fa834132a23afa1b15213caed637b96b6ebf863914fd6a2a69a4e94b3bd36"
SELECTED_AT = datetime(2026, 7, 31, 0, 5, tzinfo=UTC)


def main() -> None:
    registry = ContractIdentityRegistry.model_validate_json(
        (
            ROOT
            / "data"
            / "manifests"
            / "contract_identity_registry"
            / f"{REGISTRY_HASH}.json"
        ).read_text(encoding="utf-8")
    )
    period = (SELECTED_AT - timedelta(days=1)).strftime("%Y-%m")
    manifest_index: dict[tuple[str, str], ArchiveNormalizationManifest] = {}
    for path in (ROOT / "data" / "manifests" / "archive_normalization").glob(
        "*/*/*.json"
    ):
        manifest = ArchiveNormalizationManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if manifest.period == period:
            key = (manifest.symbol, manifest.interval)
            existing = manifest_index.get(key)
            if existing is not None and existing != manifest:
                raise RuntimeError(f"conflicting normalization manifests for {key}")
            manifest_index[key] = manifest

    frames: dict[str, pd.DataFrame] = {}
    evidence: dict[str, dict[str, str]] = {}
    mature_at = SELECTED_AT - timedelta(days=90)
    for entry in registry.entries:
        if (
            entry.effective_from > SELECTED_AT
            or (entry.effective_to is not None and SELECTED_AT >= entry.effective_to)
            or entry.onboard_date > mature_at
            or entry.derived_first_candle_at > mature_at
        ):
            continue
        interval_manifests = {
            interval: manifest_index.get((entry.symbol, interval))
            for interval in REQUIRED_INTERVALS
        }
        evidence[entry.symbol] = {
            interval: manifest.normalized_content_hash
            for interval, manifest in interval_manifests.items()
            if manifest is not None
        }
        ranking = interval_manifests["15m"]
        if ranking is not None:
            frames[entry.symbol] = pd.read_parquet(
                ROOT / "data" / "normalized" / ranking.output_relative_path
            )

    ledger = ExclusionLedger(ledger_version="empty-ledger/0.1.0", entries=())
    first = build_daily_universe(
        selected_at=SELECTED_AT,
        registry=registry,
        exclusion_ledger=ledger,
        fifteen_minute_candles=frames,
        interval_evidence=evidence,
    )
    repeated = build_daily_universe(
        selected_at=SELECTED_AT,
        registry=registry,
        exclusion_ledger=ledger,
        fifteen_minute_candles=frames,
        interval_evidence=evidence,
    )
    if first != repeated:
        raise RuntimeError("repeated real-data Universe snapshot changed")
    snapshot_path = write_universe_snapshot(first, ROOT / "data")
    print(
        json.dumps(
            {
                "selected_at": SELECTED_AT.isoformat(),
                "universe_version": first.universe_version,
                "candle_dataset_hash": first.candle_dataset_hash,
                "member_count": first.member_count,
                "members": [member.symbol for member in first.members],
                "blocked_count": len(first.blocked_candidates),
                "repeatable": True,
                "snapshot_path": str(snapshot_path.relative_to(ROOT)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
