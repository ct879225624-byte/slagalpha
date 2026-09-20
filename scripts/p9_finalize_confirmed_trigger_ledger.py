"""Add the missing point-in-time 1H structure evidence to the frozen ledger."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from decimal import Decimal

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.dev_source_scan import (
    ApproximateTickSizeImpact,
    ConfirmedTriggerLedger,
    ConfirmedTriggerRecord,
    ConfirmedTriggerSourcePartition,
    DevSourceScanReport,
    _available_structure,
    _load_symbol_sources,
    _prepare_stream,
    _slice,
    _visible_end,
    _IncrementalZoneState,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.scan_plan import DevScanPlan, model_hash
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest
from slagalpha.research.replay_inputs import DevReplayDataRequest
from slagalpha.strategy.plans import EntryStopEvaluation, EntryStopRequest, TakeProfitEvaluation
from slagalpha.strategy.pivots import PivotEvent, PivotZone
from slagalpha.strategy.setup import Direction, SetupContextEvaluation
from slagalpha.strategy.triggers import TriggerDecision, TriggerType

ROOT = Path(__file__).resolve().parents[1]
BASE_LEDGER_HASH = "f129ce6ad37eddc6fddaff01d2d352c7d9dbbde71ad068d6b8fe12e72b54e3e1"
BASE_REPORT_HASH = "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2"
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
PARAMETER_HASH = "81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b"
SCAN_PLAN_HASH = "02c709fa3dfdce157003e5695ba29eda3f446b25f125fca08ebea73d5e5fabb4"
REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"
UNIVERSE_RUN_VERSION = "d1d2d072b00341396760f7272ac5fb534bfe15dcb9fa23d60b650c21db54b32b"


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _load_context() -> tuple[
    ConfirmedTriggerLedger,
    DevSourceScanReport,
    SensitivityPlan,
    DevParameterVersion,
    ResearchSplitManifest,
    DevScanPlan,
    ContractRegistry,
    tuple[UniverseSnapshot, ...],
]:
    manifests = ROOT / "data/manifests"
    raw_ledger = json.loads(
        (manifests / "confirmed_trigger_ledger" / f"{BASE_LEDGER_HASH}.json").read_bytes()
    )
    # The source ledger is the completed 2,946-trigger result from the full scan.
    raw_ledger.pop("ledger_hash", None)
    # Old records are parsed after the 1H evidence fields are added below.
    base_report = DevSourceScanReport.model_validate_json(
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
    for path in sorted(
        (manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION).glob("*.json")
    ):
        progress = json.loads(path.read_bytes())
        snapshots.append(
            UniverseSnapshot.model_validate_json(
                (manifests / "universe_snapshot" / f"{progress['universe_version']}.json").read_bytes()
            )
        )
    # Recreate a valid source ledger object only after enriching each record.
    return (
        ConfirmedTriggerLedger.model_construct(**raw_ledger),
        base_report,
        plan,
        parameter,
        split,
        scan_plan,
        registry,
        tuple(snapshots),
    )


def _record_with_hourly_evidence(
    raw: dict[str, Any],
    *,
    streams: dict[str, Any],
    zone_state: _IncrementalZoneState,
) -> ConfirmedTriggerRecord:
    at = raw["confirmation_close_time"]
    at_dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    end = _visible_end(streams["1h"], at_dt)
    hourly, _ = _slice(streams["1h"], end, 60)
    episode_start = raw["setup_context"]["one_hour"]["episode_start"]
    episode_dt = datetime.fromisoformat(
        episode_start.replace("Z", "+00:00")
    )
    hourly_pivots, hourly_zones = _available_structure(
        streams["1h"],
        at_dt,
        zone_state,
        pivot_since=episode_dt,
        zone_since=hourly["open_time"].iloc[0].to_pydatetime(),
    )
    normalized = {
        "schema_version": raw["schema_version"],
        "symbol": raw["symbol"],
        "logical_signal_id": raw["logical_signal_id"],
        "direction": Direction(raw["direction"]),
        "primary_trigger": TriggerType(raw["primary_trigger"]),
        "confirmation_open_time": datetime.fromisoformat(
            raw["confirmation_open_time"].replace("Z", "+00:00")
        ),
        "confirmation_close_time": datetime.fromisoformat(
            raw["confirmation_close_time"].replace("Z", "+00:00")
        ),
        "confirmation_close_price": Decimal(raw["confirmation_close_price"]),
        "trigger_decision": TriggerDecision.model_validate(raw["trigger_decision"]),
        "setup_context": SetupContextEvaluation.model_validate(raw["setup_context"]),
        "visible_pivots": tuple(PivotEvent.model_validate(item) for item in raw["visible_pivots"]),
        "visible_zones": tuple(PivotZone.model_validate(item) for item in raw["visible_zones"]),
        "visible_hourly_pivots": hourly_pivots,
        "visible_hourly_zones": hourly_zones,
        "atr_at_confirmation": Decimal(raw["atr_at_confirmation"]),
        "atr_timestamp": datetime.fromisoformat(raw["atr_timestamp"].replace("Z", "+00:00")),
        "invalidation_price": Decimal(raw["invalidation_price"]),
        "structure_context": raw["structure_context"],
        "tick_size": Decimal(raw["tick_size"]),
        "tick_size_rule_content_hash": raw["tick_size_rule_content_hash"],
        "tick_size_verification_status": raw["tick_size_verification_status"],
        "tick_size_confidence": raw["tick_size_confidence"],
        "tick_size_warning_codes": tuple(raw["tick_size_warning_codes"]),
        "approximate_tick_impact": (
            ApproximateTickSizeImpact.model_validate(raw["approximate_tick_impact"])
            if raw["approximate_tick_impact"] is not None else None
        ),
        "entry_stop_request": EntryStopRequest.model_validate(raw["entry_stop_request"]),
        "baseline_entry_stop": EntryStopEvaluation.model_validate(raw["baseline_entry_stop"]),
        "baseline_take_profit": (
            TakeProfitEvaluation.model_validate(raw["baseline_take_profit"])
            if raw["baseline_take_profit"] is not None else None
        ),
        "baseline_data_request": (
            DevReplayDataRequest.model_validate(raw["baseline_data_request"])
            if raw["baseline_data_request"] is not None else None
        ),
        "baseline_p6_status": raw["baseline_p6_status"],
        "baseline_rejection_reasons": (
            tuple(raw["baseline_rejection_reasons"])
            if raw["baseline_p6_status"] != "ACCEPTED_PLAN" else ()
        ),
        "universe_content_hash": raw["universe_content_hash"],
        "source_inventory_hash": raw["source_inventory_hash"],
        "source_partitions": tuple(
            ConfirmedTriggerSourcePartition.model_validate(item)
            for item in raw["source_partitions"]
        ),
        "scan_plan_hash": raw["scan_plan_hash"],
        "split_hash": raw["split_hash"],
        "sensitivity_plan_hash": raw["sensitivity_plan_hash"],
        "parameter_content_hash": raw["parameter_content_hash"],
        "registry_content_hash": raw["registry_content_hash"],
        "algorithm_version": raw["algorithm_version"],
        "strategy_version": raw["strategy_version"],
        "trigger_version": raw["trigger_version"],
        "setup_version": raw["setup_version"],
        "trade_plan_version": raw["trade_plan_version"],
        "pivot_version": raw["pivot_version"],
        "zone_version": raw["zone_version"],
    }
    seed = ConfirmedTriggerRecord.model_construct(**normalized, record_hash="0" * 64)
    payload = seed.model_dump(mode="json", exclude={"record_hash"})
    return ConfirmedTriggerRecord.model_validate({
        **payload,
        "record_hash": _hash(payload),
    })


def main() -> int:
    base_ledger, base_report, plan, parameter, split, scan_plan, registry, snapshots = _load_context()
    raw = json.loads(
        (ROOT / "data/manifests/confirmed_trigger_ledger" / f"{BASE_LEDGER_HASH}.json").read_bytes()
    )
    source_counts = raw["symbol_confirmed_counts"]
    raw_records = raw["records"]
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in raw_records:
        by_symbol[item["symbol"]].append(item)
    final_period = scan_plan.days[-1].end_exclusive.strftime("%Y-%m")
    left, right = parameter.candidate.parameters.pivot_window
    enriched: list[ConfirmedTriggerRecord] = []
    for position, symbol in enumerate(sorted(by_symbol), start=1):
        catalog = _load_symbol_sources(ROOT, symbol, final_period=final_period)
        rule = next(item for item in registry.entries if item.symbol == symbol)
        streams = {
            interval: _prepare_stream(
                ROOT,
                catalog[interval],
                end_exclusive=scan_plan.days[-1].end_exclusive,
                lifecycle_start=rule.derived_first_candle_at,
                pivot_window=(left, right),
            )
            for interval in ("15m", "1h", "4h", "1d")
        }
        state = _IncrementalZoneState()
        for item in sorted(by_symbol[symbol], key=lambda value: value["confirmation_close_time"]):
            enriched.append(_record_with_hourly_evidence(item, streams=streams, zone_state=state))
        print(json.dumps({"symbols_completed": position, "symbols_total": len(by_symbol), "symbol": symbol}, sort_keys=True), flush=True)
    enriched.sort(key=lambda item: (item.confirmation_close_time, item.symbol, item.logical_signal_id))
    payload = {
        "schema_version": "confirmed-trigger-ledger/0.1.0",
        "scan_plan_hash": base_ledger.scan_plan_hash,
        "split_hash": base_ledger.split_hash,
        "sensitivity_plan_hash": base_ledger.sensitivity_plan_hash,
        "parameter_content_hash": base_ledger.parameter_content_hash,
        "registry_content_hash": base_ledger.registry_content_hash,
        "source_inventory_hash": base_ledger.source_inventory_hash,
        "algorithm_version": base_ledger.algorithm_version,
        "record_count": len(enriched),
        "scanned_symbols": base_report.scanned_symbols,
        "symbol_confirmed_counts": {
            symbol: sum(item.symbol == symbol for item in enriched)
            for symbol in base_report.scanned_symbols
        },
        "records": [item.model_dump(mode="json") for item in enriched],
    }
    ledger = ConfirmedTriggerLedger.model_validate({**payload, "ledger_hash": _hash(payload)})
    from slagalpha.research.dev_source_scan import write_confirmed_trigger_ledger

    ledger_path = write_confirmed_trigger_ledger(ledger, ROOT / "data")
    old_requests = {item.request.request.armed.logical_signal_id for item in base_report.accepted_requests}
    new_requests = {
        item.baseline_data_request.request.armed.logical_signal_id
        for item in enriched
        if item.baseline_data_request is not None
    }
    reconciliation = {
        "schema_version": "confirmed-trigger-ledger-reconciliation/0.1.0",
        "ledger_hash": ledger.ledger_hash,
        "source_report_hash": base_report.report_hash,
        "ledger_record_count": ledger.record_count,
        "expected_record_count": 2946,
        "symbol_counts_match": ledger.symbol_confirmed_counts == source_counts,
        "p6_status_counts": dict(sorted(Counter(item.baseline_p6_status for item in enriched).items())),
        "funnel": base_report.funnel.model_dump(mode="json"),
        "accepted_logical_signal_ids_match": new_requests == old_requests,
        "source_ledger_hash": BASE_LEDGER_HASH,
        "status": "PASS",
    }
    reconciliation["status"] = (
        "PASS"
        if ledger.record_count == 2946
        and reconciliation["symbol_counts_match"]
        and reconciliation["p6_status_counts"] == {
            "ACCEPTED_PLAN": 27,
            "REJECTED_ENTRY_STOP": 2294,
            "REJECTED_TAKE_PROFIT": 625,
        }
        and reconciliation["accepted_logical_signal_ids_match"]
        else "FAIL_CLOSED"
    )
    reconciliation["reconciliation_hash"] = _hash(reconciliation)
    reconciliation_path = (
        ROOT / "data/manifests/confirmed_trigger_ledger" / f"{ledger.ledger_hash}.reconciliation.json"
    )
    content = canonical_json_bytes(reconciliation)
    if reconciliation_path.exists() and reconciliation_path.read_bytes() != content:
        raise RuntimeError(f"immutable reconciliation changed: {reconciliation_path}")
    reconciliation_path.parent.mkdir(parents=True, exist_ok=True)
    if not reconciliation_path.exists():
        reconciliation_path.write_bytes(content)
    print(json.dumps({
        "status": reconciliation["status"],
        "ledger_hash": ledger.ledger_hash,
        "ledger_path": str(ledger_path.relative_to(ROOT)),
        "reconciliation_path": str(reconciliation_path.relative_to(ROOT)),
        "record_count": ledger.record_count,
        "p6_status_counts": reconciliation["p6_status_counts"],
        "symbol_counts_match": reconciliation["symbol_counts_match"],
        "accepted_logical_signal_ids_match": reconciliation["accepted_logical_signal_ids_match"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if reconciliation["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
