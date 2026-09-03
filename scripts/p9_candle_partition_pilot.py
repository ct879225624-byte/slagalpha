"""Revalidate four saved BTCUSDT January 2024 partitions; never run indicators or strategy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from slagalpha.data.archive import ArchiveDownloadManifest, ArchiveSpec
from slagalpha.data.klines import ArchiveNormalizationManifest
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.candle_inputs import CandlePartitionSource, load_verified_candle_partition
from slagalpha.research.scan_plan import model_hash

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    manifests = ROOT / "data/manifests"
    results = []
    for interval in ("15m", "1h", "4h", "1d"):
        spec = ArchiveSpec.model_validate({
            "symbol": "BTCUSDT", "interval": interval, "year": 2024, "month": 1,
        })
        matches = []
        for path in sorted((manifests / "archive_normalization" / spec.symbol / interval).glob(
            "*.json"
        )):
            item = ArchiveNormalizationManifest.model_validate_json(path.read_bytes())
            if (item.symbol, item.interval, item.period) == (spec.symbol, interval, spec.period):
                matches.append(item)
        if len(matches) != 1:
            raise ValueError("pilot requires exactly one saved normalization receipt per partition")
        manifest = matches[0]
        download_path = (manifests / "archive_download" / spec.symbol / interval
                         / f"{manifest.source_file_hash}.json")
        source = CandlePartitionSource(
            spec=spec, normalization=manifest,
            download=ArchiveDownloadManifest.model_validate_json(download_path.read_bytes()),
        )
        frame = load_verified_candle_partition(project_dir=ROOT, source=source)
        results.append({
            "identity": f"{spec.symbol}/{interval}/{spec.period}", "row_count": len(frame),
            "source_binding_hash": model_hash(source),
            "normalized_content_hash": manifest.normalized_content_hash,
            "raw_sha256": manifest.source_file_hash, "parquet_sha256": manifest.parquet_sha256,
        })
    payload = {"schema_version": "candle-partition-pilot/0.1.0", "partitions": results,
               "strategy_executed": False, "research_authorized": False}
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    report = {**payload, "report_hash": digest}
    destination = manifests / "candle_partition_pilot" / f"{digest}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, canonical_json_bytes(report))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
