"""Audit the 27 real normalization failures against Universe lookback dependencies."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.domain.universe import UniverseSnapshot
from slagalpha.research.normalization_gaps import (
    build_normalization_gap_audit,
    write_normalization_gap_audit,
)

ROOT = Path(__file__).resolve().parents[1]
NORMALIZATION_HASH = "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
UNIVERSE_RUN_VERSION = "d1d2d072b00341396760f7272ac5fb534bfe15dcb9fa23d60b650c21db54b32b"
DAILY_SNAPSHOT_HASH = "2ae73f816286e7932f88d2e7c8859a3c5ad8146df4b0ae59dc242faaae1412f8"


def main() -> int:
    manifests = ROOT / "data" / "manifests"
    normalization = NormalizationBatchResult.model_validate_json(
        (manifests / "normalization_batch" / f"{NORMALIZATION_HASH}.json").read_bytes()
    )
    snapshots = []
    progress_dir = manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION
    for progress_path in sorted(progress_dir.glob("*.json")):
        progress = json.loads(progress_path.read_bytes())
        snapshots.append(UniverseSnapshot.model_validate_json(
            (manifests / "universe_snapshot" / f"{progress['universe_version']}.json").read_bytes()
        ))
    report = build_normalization_gap_audit(
        normalization=normalization,
        snapshots=tuple(snapshots),
        expected_daily_snapshot_hash=DAILY_SNAPSHOT_HASH,
        monthly_klines_dir=ROOT / "data" / "raw" / "binance" / "futures" / "um"
        / "monthly" / "klines",
    )
    path = write_normalization_gap_audit(report, ROOT / "data")
    print(json.dumps({
        "status": report.status,
        "report_hash": report.report_hash,
        "failure_file_count": report.failure_file_count,
        "verified_gap_file_count": report.verified_gap_file_count,
        "direct_overlap_day_count": report.direct_overlap_day_count,
        "lookback_overlap_day_count": report.lookback_overlap_day_count,
        "recursive_history_overlap_day_count": report.recursive_history_overlap_day_count,
        "finite_lookback_bars": report.finite_lookback_bars,
        "blockers": list(report.blockers),
        "semantic_gate_relaxation_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "report_path": str(path.relative_to(ROOT)),
    }, sort_keys=True))
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
