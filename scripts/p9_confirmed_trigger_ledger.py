"""Regenerate the frozen baseline P5 evidence into an immutable trigger ledger."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.dev_source_scan import (
    ConfirmedTriggerLedger,
    ConfirmedTriggerRecord,
    DevSourceScanReport,
    build_dev_source_scan_report,
    write_confirmed_trigger_ledger,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest

ROOT = Path(__file__).resolve().parents[1]
BASE_REPORT_HASH = "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2"
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
PARAMETER_HASH = "81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b"
SCAN_PLAN_HASH = "02c709fa3dfdce157003e5695ba29eda3f446b25f125fca08ebea73d5e5fabb4"
REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"
UNIVERSE_RUN_VERSION = "d1d2d072b00341396760f7272ac5fb534bfe15dcb9fa23d60b650c21db54b32b"
OLD_CHECKPOINT_DIR = ROOT / "data/checkpoints/dev_source_scan/3239205b07801da5a1296daa76f5de92fa35d091410713d45e8a57bad57bd783"
LEDGER_CHECKPOINT_DIR = ROOT / "data/checkpoints/confirmed_trigger_ledger_scan_v4"


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _load_context() -> tuple[
    DevSourceScanReport,
    SensitivityPlan,
    DevParameterVersion,
    ResearchSplitManifest,
    DevScanPlan,
    ContractRegistry,
    tuple[UniverseSnapshot, ...],
]:
    manifests = ROOT / "data/manifests"
    report = DevSourceScanReport.model_validate_json(
        (manifests / "dev_source_scan" / f"{BASE_REPORT_HASH}.json").read_bytes()
    )
    plan = SensitivityPlan.model_validate_json(
        (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    parameter = DevParameterVersion.model_validate_json(
        (manifests / "parameter_version" / f"{PARAMETER_HASH}.json").read_bytes()
    )
    split = ResearchSplitManifest.model_validate_json(
        (manifests / "research_split" / f"{plan.split_hash}.json").read_bytes()
    )
    scan_plan = DevScanPlan.model_validate_json(
        (manifests / "dev_scan_plan" / f"{SCAN_PLAN_HASH}.json").read_bytes()
    )
    registry = ContractRegistry.model_validate_json(
        (manifests / "contract_registry" / f"{REGISTRY_HASH}.json").read_bytes()
    )
    snapshots = []
    for progress_path in sorted(
        (manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION).glob("*.json")
    ):
        progress = json.loads(progress_path.read_bytes())
        snapshots.append(
            UniverseSnapshot.model_validate_json(
                (manifests / "universe_snapshot" / f"{progress['universe_version']}.json").read_bytes()
            )
        )
    return report, plan, parameter, split, scan_plan, registry, tuple(snapshots)


def _original_symbol_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    paths = sorted(OLD_CHECKPOINT_DIR.glob("*.json"))
    if len(paths) != 248:
        raise RuntimeError(f"expected 248 original symbol checkpoints, found {len(paths)}")
    for path in paths:
        payload = json.loads(path.read_bytes())
        if payload.get("symbol") in counts:
            raise RuntimeError(f"duplicate original symbol checkpoint: {payload.get('symbol')}")
        counts[payload["symbol"]] = int(payload["outcome_counts"].get("trigger_confirmed_count", 0))
    return dict(sorted(counts.items()))


def _write_reconciliation(ledger_hash: str, payload: dict[str, Any]) -> Path:
    content = canonical_json_bytes(payload)
    digest = _hash({key: value for key, value in payload.items() if key != "reconciliation_hash"})
    destination = ROOT / "data/manifests/confirmed_trigger_ledger" / f"{ledger_hash}.reconciliation.json"
    if destination.exists() and destination.read_bytes() != content:
        raise RuntimeError(f"immutable reconciliation changed: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(content)
    if digest != payload["reconciliation_hash"]:
        raise RuntimeError("reconciliation hash mismatch")
    return destination


def main() -> int:
    base_report, plan, parameter, split, scan_plan, registry, snapshots = _load_context()
    original_symbol_counts = _original_symbol_counts()
    records: list[ConfirmedTriggerRecord] = []

    def sink(record: ConfirmedTriggerRecord) -> None:
        records.append(record)

    def progress(position: int, total: int, symbol: str, slots: int, accepted: int) -> None:
        print(json.dumps({
            "symbols_completed": position,
            "symbols_total": total,
            "symbol": symbol,
            "symbol_slots": slots,
            "symbol_accepted": accepted,
            "ledger_records": len(records),
        }, sort_keys=True), flush=True)

    report = build_dev_source_scan_report(
        project_dir=ROOT,
        scan_plan=scan_plan,
        split=split,
        plan=plan,
        parameter=parameter,
        snapshots=snapshots,
        registry=registry,
        checkpoint_dir=LEDGER_CHECKPOINT_DIR,
        progress=progress,
        confirmed_trigger_sink=sink,
    )
    records.sort(key=lambda item: (
        item.confirmation_close_time, item.symbol, item.logical_signal_id
    ))
    ledger_payload = {
        "schema_version": "confirmed-trigger-ledger/0.1.0",
        "scan_plan_hash": scan_plan.plan_hash,
        "split_hash": split.split_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "parameter_content_hash": parameter.content_hash,
        "registry_content_hash": report.registry_content_hash,
        "source_inventory_hash": report.source_inventory_hash,
        "algorithm_version": "dev-source-scan-algorithm/0.1.1",
        "record_count": len(records),
        "scanned_symbols": report.scanned_symbols,
        "symbol_confirmed_counts": {
            symbol: sum(item.symbol == symbol for item in records)
            for symbol in report.scanned_symbols
        },
        "records": [item.model_dump(mode="json") for item in records],
    }
    ledger = ConfirmedTriggerLedger.model_validate({
        **ledger_payload,
        "ledger_hash": _hash(ledger_payload),
    })
    ledger_path = write_confirmed_trigger_ledger(ledger, ROOT / "data")

    old_request_payload = {
        "accepted_requests": [item.model_dump(mode="json") for item in base_report.accepted_requests]
    }
    new_request_payload = {
        "accepted_requests": [item.model_dump(mode="json") for item in report.accepted_requests]
    }
    old_accepted_ids = sorted(item.request.request.armed.logical_signal_id for item in base_report.accepted_requests)
    new_accepted_ids = sorted(item.request.request.armed.logical_signal_id for item in report.accepted_requests)
    status_counts = dict(sorted(Counter(item.baseline_p6_status for item in records).items()))
    reconciliation = {
        "schema_version": "confirmed-trigger-ledger-reconciliation/0.1.0",
        "ledger_hash": ledger.ledger_hash,
        "source_report_hash": base_report.report_hash,
        "regenerated_report_hash": report.report_hash,
        "ledger_record_count": ledger.record_count,
        "expected_record_count": 2946,
        "symbol_counts_match": ledger.symbol_confirmed_counts == original_symbol_counts,
        "original_symbol_confirmed_counts_hash": _hash(original_symbol_counts),
        "ledger_symbol_confirmed_counts_hash": _hash(ledger.symbol_confirmed_counts),
        "funnel": report.funnel.model_dump(mode="json"),
        "p6_status_counts": status_counts,
        "expected_p6_status_counts": {
            "REJECTED_ENTRY_STOP": 2294,
            "REJECTED_TAKE_PROFIT": 625,
            "ACCEPTED_PLAN": 27,
        },
        "accepted_request_set_hash": report.request_set_hash,
        "source_accepted_request_set_hash": base_report.request_set_hash,
        "accepted_request_set_match": new_request_payload == old_request_payload,
        "accepted_logical_signal_ids_match": new_accepted_ids == old_accepted_ids,
        "report_hash_match": report.report_hash == base_report.report_hash,
        "status": "PASS",
    }
    reconciliation["status"] = (
        "PASS"
        if (
            ledger.record_count == 2946
            and reconciliation["symbol_counts_match"]
            and status_counts == reconciliation["expected_p6_status_counts"]
            and report.funnel.entry_stop_rejected_count == 2294
            and report.funnel.take_profit_rejected_count == 625
            and report.funnel.accepted_count == 27
            and report.funnel.request_boundary_rejected_count == 0
            and reconciliation["accepted_request_set_match"]
            and reconciliation["accepted_logical_signal_ids_match"]
            and reconciliation["report_hash_match"]
        )
        else "FAIL_CLOSED"
    )
    reconciliation["reconciliation_hash"] = _hash(reconciliation)
    reconciliation_path = _write_reconciliation(ledger.ledger_hash, reconciliation)
    output = {
        "status": reconciliation["status"],
        "ledger_hash": ledger.ledger_hash,
        "ledger_path": str(ledger_path.relative_to(ROOT)),
        "reconciliation_path": str(reconciliation_path.relative_to(ROOT)),
        "record_count": ledger.record_count,
        "funnel": report.funnel.model_dump(mode="json"),
        "p6_status_counts": status_counts,
        "symbol_counts_match": reconciliation["symbol_counts_match"],
        "accepted_request_set_match": reconciliation["accepted_request_set_match"],
        "report_hash_match": reconciliation["report_hash_match"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if reconciliation["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
