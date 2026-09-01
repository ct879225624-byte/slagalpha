"""Tests for resumable historical daily Universe batches."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from slagalpha.data.klines import ArchiveNormalizationManifest
from slagalpha.data.universe_batch import run_universe_batch
from slagalpha.domain.universe import (
    ContractIdentityLifecycleEntry,
    ContractIdentityRegistry,
    EvidenceConfidence,
    ExclusionLedger,
    RegistryVerification,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _registry() -> ContractIdentityRegistry:
    onboard = datetime(2023, 1, 1, tzinfo=UTC)
    entry = ContractIdentityLifecycleEntry(
        symbol="AAAUSDT",
        base_asset="AAA",
        quote_asset="USDT",
        margin_asset="USDT",
        contract_type="PERPETUAL",
        onboard_date=onboard,
        derived_first_candle_at=onboard,
        effective_from=onboard,
        effective_to=None,
        source_ref="fixture:identity",
        source_snapshot_hash=_hash("identity"),
        reviewed_by="TEST",
        verification_status=RegistryVerification.VERIFIED,
        confidence=EvidenceConfidence.HIGH,
    )
    return ContractIdentityRegistry(registry_version="identity-v1", entries=(entry,))


def _write_fixture(data_dir: Path) -> None:
    opens = pd.date_range(
        datetime(2024, 1, 1, tzinfo=UTC),
        periods=192,
        freq="15min",
    )
    relative = Path("klines") / "AAAUSDT" / "15m" / "fixture.parquet"
    destination = data_dir / "normalized" / relative
    destination.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "open_time": opens,
            "close_time_exclusive": opens + pd.Timedelta(minutes=15),
            "quote_volume": ["10"] * len(opens),
            "is_closed": [True] * len(opens),
            "source_file_hash": [_hash("source")] * len(opens),
        }
    ).to_parquet(destination, index=False)

    for interval in ("15m", "1h", "4h", "1d"):
        manifest = ArchiveNormalizationManifest(
            source_file_hash=_hash(f"source:{interval}"),
            symbol="AAAUSDT",
            interval=interval,
            period="2024-01",
            parsed_row_count=192,
            normalized_row_count=192,
            identical_duplicates_removed=0,
            first_open_time=opens[0].to_pydatetime(),
            last_open_time=opens[-1].to_pydatetime(),
            normalized_content_hash=_hash(f"normalized:{interval}"),
            parquet_sha256=_hash(f"parquet:{interval}"),
            output_relative_path=str(relative).replace("\\", "/"),
            normalized_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
        path = (
            data_dir
            / "manifests"
            / "archive_normalization"
            / "AAAUSDT"
            / interval
            / f"{manifest.source_file_hash}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest.model_dump(mode="json")),
            encoding="utf-8",
        )


def test_batch_writes_two_days_then_reuses_exact_snapshots(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    ledger = ExclusionLedger(ledger_version="empty-v1", entries=())

    first = run_universe_batch(
        registry=_registry(),
        exclusion_ledger=ledger,
        dataset_content_hash=_hash("dataset"),
        selection_start=date(2024, 1, 2),
        selection_end_exclusive=date(2024, 1, 4),
        normalized_data_dir=tmp_path / "normalized",
        manifests_dir=tmp_path / "manifests",
    )
    repeated = run_universe_batch(
        registry=_registry(),
        exclusion_ledger=ledger,
        dataset_content_hash=_hash("dataset"),
        selection_start=date(2024, 1, 2),
        selection_end_exclusive=date(2024, 1, 4),
        normalized_data_dir=tmp_path / "normalized",
        manifests_dir=tmp_path / "manifests",
    )
    verification = run_universe_batch(
        registry=_registry(),
        exclusion_ledger=ledger,
        dataset_content_hash=_hash("dataset"),
        selection_start=date(2024, 1, 2),
        selection_end_exclusive=date(2024, 1, 4),
        normalized_data_dir=tmp_path / "normalized",
        manifests_dir=tmp_path / "manifests",
        progress_namespace="determinism-replay-2",
    )
    resumed_verification = run_universe_batch(
        registry=_registry(),
        exclusion_ledger=ledger,
        dataset_content_hash=_hash("dataset"),
        selection_start=date(2024, 1, 2),
        selection_end_exclusive=date(2024, 1, 4),
        normalized_data_dir=tmp_path / "normalized",
        manifests_dir=tmp_path / "manifests",
        progress_namespace="determinism-replay-2",
    )

    assert first.complete is True
    assert first.computed_count == 2
    assert first.reused_count == 0
    assert first.total_member_count == 2
    assert repeated.computed_count == 0
    assert repeated.reused_count == 2
    assert repeated.daily_snapshot_hash == first.daily_snapshot_hash
    assert repeated.result_hash != first.result_hash
    assert verification.computed_count == 2
    assert verification.daily_snapshot_hash == first.daily_snapshot_hash
    assert verification.progress_version != first.progress_version
    assert resumed_verification.reused_count == 2
    assert len(tuple((tmp_path / "manifests" / "universe_snapshot").glob("*.json"))) == 2
