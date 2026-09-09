"""Audit lifecycle warm-up evidence without running indicators or research."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from slagalpha.data.archive import ArchiveDownloadManifest, ArchiveSpec
from slagalpha.data.klines import ArchiveNormalizationManifest
from slagalpha.research.atr_history_audit import (
    Interval,
    build_atr_history_seed_audit,
    load_verified_lifecycle_warmup_input,
    write_atr_history_seed_audit,
)
from slagalpha.research.candle_inputs import CandlePartitionSource
from slagalpha.research.lifecycle_real_execution import LifecycleRealExecutionReceipt
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan
from slagalpha.research.lifecycle_replacement import LifecycleReplacementNormalizationResult

ROOT = Path(__file__).resolve().parents[1]
REPLACEMENT_HASH = "7565da18af89195250651ba2cf49f60ae4f8725ce9a83a2b010e540e5047911b"
PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
RECEIPT_HASH = "3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e"


def _next_period(period: str) -> str:
    at = datetime.strptime(period, "%Y-%m")
    year = at.year + 1 if at.month == 12 else at.year
    return f"{year:04d}-{at.month % 12 + 1:02d}"


def _saved_source(symbol: str, interval: Interval, period: str) -> CandlePartitionSource | None:
    manifests = ROOT / "data/manifests"
    matches = []
    for path in sorted((manifests / "archive_normalization" / symbol / interval).glob("*.json")):
        item = ArchiveNormalizationManifest.model_validate_json(path.read_bytes())
        if (item.symbol, item.interval, item.period) == (symbol, interval, period):
            matches.append(item)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("warm-up audit requires one normalization receipt per partition")
    normalization = matches[0]
    year, month = (int(part) for part in period.split("-"))
    download = ArchiveDownloadManifest.model_validate_json(
        (
            manifests
            / "archive_download"
            / symbol
            / interval
            / f"{normalization.source_file_hash}.json"
        ).read_bytes()
    )
    return CandlePartitionSource(
        spec=ArchiveSpec(symbol=symbol, interval=interval, year=year, month=month),
        download=download,
        normalization=normalization,
    )


def main() -> int:
    manifests = ROOT / "data" / "manifests"
    replacement = LifecycleReplacementNormalizationResult.model_validate_json(
        (
            manifests
            / "lifecycle_replacement_normalization"
            / f"{REPLACEMENT_HASH}.json"
        ).read_bytes()
    )
    plan = LifecycleNormalizationRemediationPlan.model_validate_json(
        (manifests / "lifecycle_normalization_remediation_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    receipt = LifecycleRealExecutionReceipt.model_validate_json(
        (manifests / "lifecycle_real_execution" / f"{RECEIPT_HASH}.json").read_bytes()
    )
    outputs = {f"{item.symbol}/{item.interval}": item for item in receipt.outputs}
    inputs = []
    for action in plan.actions:
        output = outputs[f"{action.symbol}/{action.interval}"]
        sources = []
        declared_rows = output.retained_row_count
        period = _next_period(action.evidence_period)
        while declared_rows < 180:
            source = _saved_source(action.symbol, action.interval, period)
            if source is None:
                break
            sources.append(source)
            declared_rows += source.normalization.normalized_row_count
            period = _next_period(period)
        inputs.append(load_verified_lifecycle_warmup_input(
            project_dir=ROOT,
            action=action,
            output=output,
            subsequent_sources=tuple(sources),
        ))
    report = build_atr_history_seed_audit(
        replacement=replacement,
        expected_replacement_hash=REPLACEMENT_HASH,
        inputs=tuple(inputs),
        unresolved_lifecycle_identities=replacement.remaining_failure_identities,
    )
    path = write_atr_history_seed_audit(report, ROOT / "data")
    print(
        json.dumps(
            {**report.model_dump(mode="json"), "report_path": str(path.relative_to(ROOT))},
            sort_keys=True,
        )
    )
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
