"""Execute the authorized Phase 3 Stage B replay comparison."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx

from slagalpha.backtest.costs import CostScenario
from slagalpha.data.binance_usdm_transport import (
    BinanceTransportPlan,
    run_binance_transport,
)
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.batch_replay import run_replay_batch
from slagalpha.research.dev_source_scan import (
    AcceptedP6Request,
    DevMarketDataRequirement,
    DevScanFunnel,
    DevSourceScanReport,
    validate_execution_checkpoint,
)
from slagalpha.research.funding_mark_supplement import FundingMarkSupplementArtifact
from slagalpha.research.market_data_requirements import (
    _merged_windows,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_market_data import ReplayMarketDataArtifact
from slagalpha.research.replay_prep import (
    ReplayPrepResult,
    prepare_dev_replay,
    write_replay_prep_artifacts,
)
from slagalpha.research.rescan_stage_a import BASELINE_CANDIDATE_HASH, StageAFunnelArtifact
from slagalpha.research.scan_plan import model_hash
from slagalpha.research.sensitivity import SensitivityPlan

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
STAGE_A_SUMMARY = (
    ROOT / "artifacts/sens_p3_stage_a_summary/"
    "b27864dc0fab85ad56e486c080bc5a03882cb6cea8ae1b2a0f49c3c12db50f61.json"
)
STAGE_A_DIR = ROOT / "artifacts/sens_p3_stage_a"
OUT_ROOT = ROOT / "artifacts/sens_p3_stage_b"
BASE_REPORT_HASH = "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2"
BASE_TRANSPORT_HASH = "d7915197e19d47aa86904d9d9cd184fc59e5df580e0e8282e8f89fd13259c769"
BASELINE_REPLAY_DIRS = {
    CostScenario.ZERO: ROOT / "artifacts/dev_replay_baseline_ZERO_20260913",
    CostScenario.BASELINE: ROOT / "artifacts/dev_replay_baseline_BASELINE_20260913",
    CostScenario.STRESS: ROOT / "artifacts/dev_replay_baseline_STRESS_20260913",
}
CANDIDATES = {
    "pivot_window_3x3": "d889a882a5f551931d741752b28b65e1779cc3d247729abff249a221d6b976c4",
    "compression_threshold_0.50": (
        "def218e7a145e94ae8c08f2a601f337e1f53f871e7bda96339514af9f8c3a706"
    ),
    "compression_threshold_1.00": (
        "15ca63f9a27f254d6cc0117b586e58399d500893ccd263ac6729cc09752f064f"
    ),
}


def _load_p1() -> Any:
    path = ROOT / "scripts/p9_sensitivity_phase1.py"
    spec = importlib.util.spec_from_file_location("p9_phase1_for_phase3", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Phase 1 helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P1 = _load_p1()


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    content = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or path.read_bytes() != content):
        raise RuntimeError(f"immutable artifact changed: {path}")
    if not path.exists():
        path.write_bytes(content)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _load_stage_a(candidate_hash: str) -> StageAFunnelArtifact:
    matches = []
    for path in (STAGE_A_DIR / candidate_hash[:12]).glob("*.json"):
        artifact = StageAFunnelArtifact.model_validate_json(path.read_bytes())
        if artifact.candidate_hash == candidate_hash and artifact.status == "COMPLETE":
            matches.append(artifact)
    if len(matches) != 1:
        raise RuntimeError(f"expected one complete Stage A artifact for {candidate_hash}")
    return matches[0]


def _checkpoint_docs(artifact: StageAFunnelArtifact) -> tuple[dict[str, Any], ...]:
    root = (
        ROOT
        / "data/checkpoints/p9_stage_a"
        / artifact.candidate_hash
        / model_hash(artifact.execution_binding)
    )
    docs = []
    for path in sorted(root.glob("*.json")):
        value = _load_json(path)
        validate_execution_checkpoint(value, artifact.execution_binding)
        docs.append(value)
    if len(docs) != len(artifact.scanned_symbols):
        raise RuntimeError(f"checkpoint count mismatch for {artifact.candidate_hash}")
    if tuple(sorted(item["symbol"] for item in docs)) != artifact.scanned_symbols:
        raise RuntimeError(f"checkpoint symbols mismatch for {artifact.candidate_hash}")
    return tuple(docs)


def _accepted_from_checkpoints(docs: tuple[dict[str, Any], ...]) -> tuple[AcceptedP6Request, ...]:
    items = [
        AcceptedP6Request.model_validate(item) for doc in docs for item in doc["accepted_requests"]
    ]
    result = tuple(
        sorted(
            items,
            key=lambda item: (
                item.request.start,
                item.request.request.armed.symbol,
                item.request.request.armed.logical_signal_id,
            ),
        )
    )
    if len({item.request.request.armed.logical_signal_id for item in result}) != len(result):
        raise RuntimeError("duplicate accepted logical signal IDs")
    return result


def _report_from_checkpoints(
    artifact: StageAFunnelArtifact,
    docs: tuple[dict[str, Any], ...],
    baseline: DevSourceScanReport,
) -> DevSourceScanReport:
    accepted = _accepted_from_checkpoints(docs)
    totals: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for doc in docs:
        totals.update(doc["outcome_counts"])
        reasons.update(doc["reason_counts"])
        totals["scan_slot_count"] += int(doc["symbol_slot_count"])
    if artifact.funnel is None:
        raise RuntimeError("complete Stage A artifact lacks funnel")
    if any(totals[name] != getattr(artifact.funnel, name) for name in DevScanFunnel.model_fields):
        raise RuntimeError("checkpoint funnel does not reconcile with Stage A artifact")
    grouped: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    for item in accepted:
        request = item.request
        grouped[request.request.armed.symbol].append(
            (request.start, request.end_exclusive, request.request_hash)
        )
    merged = [(symbol, _merged_windows(values)) for symbol, values in grouped.items()]
    requirement_rows: list[dict[str, Any]] = [
        {
            "symbol": symbol,
            "windows": [item.model_dump(mode="json") for item in windows],
            "one_minute_record_count": sum(item.one_minute_record_count for item in windows),
            "funding_record_count_estimate": None,
        }
        for symbol, windows in sorted(merged)
    ]
    market_requirement = DevMarketDataRequirement(
        symbols=tuple(item["symbol"] for item in requirement_rows),
        one_minute_rows_before_overlap_dedup=sum(
            item.request.expected_candle_count for item in accepted
        ),
        one_minute_rows_after_overlap_dedup=sum(
            item["one_minute_record_count"] for item in requirement_rows
        ),
        funding_windows_before_overlap_dedup=len(accepted),
        funding_windows_after_overlap_dedup=sum(len(item["windows"]) for item in requirement_rows),
        merged_window_minutes=sum(item["one_minute_record_count"] for item in requirement_rows),
    )
    fallback_slots = sum(int(doc["fallback_rule_slot_count"]) for doc in docs)
    fallback_plans = sum(int(doc["fallback_price_plan_count"]) for doc in docs)
    approximate_prices = sum(int(doc["approximate_price_observation_count"]) for doc in docs)
    approximate_outcomes = sum(int(doc["approximate_outcome_warning_count"]) for doc in docs)
    maxima = [
        Decimal(str(doc["max_approximate_tick_adjustment_bps"]))
        for doc in docs
        if doc["max_approximate_tick_adjustment_bps"] is not None
    ]
    payload = baseline.model_dump(
        mode="json",
        exclude={
            "report_hash",
            "parameter_content_hash",
            "accepted_requests",
            "accepted_symbols",
            "request_set_hash",
            "market_data_requirement",
            "funnel",
            "reason_counts",
            "validated_source_partition_count",
            "fallback_rule_slot_count",
            "fallback_price_plan_count",
            "approximate_price_observation_count",
            "approximate_outcome_warning_count",
            "max_approximate_tick_adjustment_bps",
            "warning_codes",
            "status",
        },
    )
    payload.update(
        {
            "parameter_content_hash": artifact.parameter_content_hash,
            "accepted_requests": [item.model_dump(mode="json") for item in accepted],
            "accepted_symbols": sorted({item.request.request.armed.symbol for item in accepted}),
            "funnel": artifact.funnel.model_dump(mode="json"),
            "reason_counts": dict(sorted(reasons.items())),
            "validated_source_partition_count": sum(
                int(doc["validated_source_partition_count"]) for doc in docs
            ),
            "fallback_rule_slot_count": fallback_slots,
            "fallback_price_plan_count": fallback_plans,
            "approximate_price_observation_count": approximate_prices,
            "approximate_outcome_warning_count": approximate_outcomes,
            "max_approximate_tick_adjustment_bps": str(max(maxima)) if maxima else None,
            "warning_codes": ["APPROXIMATE_HISTORICAL_TICK_SIZE"] if fallback_slots else [],
            "request_set_hash": _hash(
                {"accepted_requests": [item.model_dump(mode="json") for item in accepted]}
            ),
            "market_data_requirement": market_requirement.model_dump(mode="json"),
            "status": "COMPLETE",
        }
    )
    report = DevSourceScanReport.model_validate({**payload, "report_hash": _hash(payload)})
    if report.request_set_hash != artifact.accepted_request_set_hash:
        raise RuntimeError(
            "reconstructed accepted request set hash does not match Stage A artifact"
        )
    return report


def _economic_payload(accepted: AcceptedP6Request) -> dict[str, Any]:
    data_request = accepted.request
    trade = data_request.request
    armed = trade.armed
    return {
        "logical_signal_id": armed.logical_signal_id,
        "symbol": armed.symbol,
        "direction": armed.direction.value,
        "confirmation_close": armed.confirmation_close.isoformat().replace("+00:00", "Z"),
        "expires_at": armed.expires_at.isoformat().replace("+00:00", "Z"),
        "entry": str(armed.entry_price),
        "stop": str(armed.stop_price),
        "invalidation": str(armed.invalidation_price),
        "atr_at_confirmation": str(armed.atr_at_confirmation),
        "tp1": str(trade.tp1),
        "tp2": str(trade.tp2),
        "tick_size": str(trade.tick_size),
        "breakeven_price": None if trade.breakeven_price is None else str(trade.breakeven_price),
        "max_holding_bars": trade.max_holding_bars,
        "plan_version": trade.plan_version,
        "replay_window": {
            "start": data_request.start.isoformat().replace("+00:00", "Z"),
            "end_exclusive": data_request.end_exclusive.isoformat().replace("+00:00", "Z"),
            "expected_candle_count": data_request.expected_candle_count,
        },
    }


def _semantic_gate(
    baseline: tuple[AcceptedP6Request, ...], candidate: tuple[AcceptedP6Request, ...]
) -> dict[str, Any]:
    left = {item.request.request.armed.logical_signal_id: item for item in baseline}
    right = {item.request.request.armed.logical_signal_id: item for item in candidate}
    if set(left) != set(right):
        return {
            "replay_semantic_equivalence": False,
            "baseline_economic_payload_hash": _hash(
                {key: _economic_payload(left[key]) for key in sorted(left)}
            ),
            "candidate_economic_payload_hash": _hash(
                {key: _economic_payload(right[key]) for key in sorted(right)}
            ),
            "field_diffs": [
                {"logical_signal_id": key, "reason": "logical_signal_id_set_diff"}
                for key in sorted(set(left) ^ set(right))
            ],
        }
    diffs = []
    for key in sorted(left):
        a = _economic_payload(left[key])
        b = _economic_payload(right[key])
        if a != b:
            fields = sorted(set(a) | set(b))
            diffs.append(
                {
                    "logical_signal_id": key,
                    "fields": {
                        field: {"baseline": a.get(field), "candidate": b.get(field)}
                        for field in fields
                        if a.get(field) != b.get(field)
                    },
                }
            )
    return {
        "replay_semantic_equivalence": not diffs,
        "baseline_economic_payload_hash": _hash(
            {key: _economic_payload(left[key]) for key in sorted(left)}
        ),
        "candidate_economic_payload_hash": _hash(
            {key: _economic_payload(right[key]) for key in sorted(right)}
        ),
        "field_diffs": diffs,
    }


def _merge_sources() -> tuple[Any, ...]:
    candles, funding, candle_intervals, funding_intervals, guard_rows, observed, supplements = (
        P1._load_baseline_sources()
    )
    roots = [
        ROOT / "artifacts/sens_p1",
        ROOT / "artifacts/sens_p2_stage_b",
        ROOT / "artifacts/sens_p2_stage_b_final",
    ]
    for root in roots:
        for path in sorted(root.glob("**/market_data/*.json")):
            artifact = ReplayMarketDataArtifact.model_validate_json(path.read_bytes())
            for response in artifact.responses:
                body = json.loads((ROOT / response.relative_path).read_text(encoding="utf-8"))
                observed[response.symbol] = max(
                    observed.get(response.symbol, response.observed_at), response.observed_at
                )
                left, right = response.start_time_ms, response.end_time_ms + 1
                target = (
                    candle_intervals if artifact.role == "CANDLE_ONE_MINUTE" else funding_intervals
                )
                target[response.symbol].append((left, right))
                if artifact.role == "CANDLE_ONE_MINUTE":
                    for row in body:
                        candles[response.symbol][int(row[0])] = row
                else:
                    for row in body:
                        funding[response.symbol][int(row["fundingTime"])] = row
        for path in sorted(root.glob("**/funding_mark_supplement/*.json")):
            item = FundingMarkSupplementArtifact.model_validate_json(path.read_bytes())
            supplements[item.request_hash] = item
    for plan_path in sorted((ROOT / "artifacts/market_data/transport_plans").glob("*.json")):
        plan = BinanceTransportPlan.model_validate_json(plan_path.read_bytes())
        receipts = (
            ROOT / "artifacts/market_data/transport_runs" / plan.transport_plan_hash / "receipts"
        )
        for task in plan.tasks:
            if task.role not in {"FUNDING_GUARD_BEFORE", "FUNDING_GUARD_AFTER"}:
                continue
            receipt = _load_json(receipts / f"{task.task_hash}.json")
            body = json.loads((ROOT / receipt["blob_relative_path"]).read_text(encoding="utf-8"))
            observed[task.symbol] = max(
                observed.get(
                    task.symbol,
                    datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00")),
                ),
                datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00")),
            )
            guard_rows[task.symbol].extend(body)
            for row in body:
                funding[task.symbol][int(row["fundingTime"])] = row
    for symbol in list(candle_intervals):
        candle_intervals[symbol] = P1._interval_union(candle_intervals[symbol])
    for symbol in list(funding_intervals):
        funding_intervals[symbol] = P1._interval_union(funding_intervals[symbol])
    return candles, funding, candle_intervals, funding_intervals, guard_rows, observed, supplements


def _retry_transport_plan(
    plan: BinanceTransportPlan, max_attempts: int = 6
) -> BinanceTransportPlan:
    tasks = tuple(
        sorted(
            (
                P1._task(
                    requirements_plan_hash=item.requirements_plan_hash,
                    role=item.role,
                    symbol=item.symbol,
                    start=datetime.fromtimestamp(item.start_time_ms / 1000, tz=UTC),
                    end_exclusive=datetime.fromtimestamp((item.end_time_ms + 1) / 1000, tz=UTC),
                    request_hashes=item.accepted_request_hashes,
                    max_attempts=max_attempts,
                )
                for item in plan.tasks
            ),
            key=lambda item: item.task_hash,
        )
    )
    payload = {
        "schema_version": plan.schema_version,
        "requirements_plan_hash": plan.requirements_plan_hash,
        "source_report_hash": plan.source_report_hash,
        "request_set_hash": plan.request_set_hash,
        "accepted_request_hashes": plan.accepted_request_hashes,
        "tasks": [item.model_dump(mode="json") for item in tasks],
        "network_authorized": False,
    }
    return BinanceTransportPlan.model_validate({**payload, "transport_plan_hash": _hash(payload)})


def _patch_flat_trade_rows(summary_module: Any) -> None:
    original = summary_module._trade_row

    def safe_trade_row(result: Any) -> dict[str, Any]:
        if result.gross_r != 0:
            return cast(dict[str, Any], original(result))
        if result.p7_summary is None:
            raise RuntimeError("flat result is missing P7 summary")
        canonical = json.loads(result.p7_summary.canonical_json)
        risk = abs(Decimal(canonical["plan"]["entry"]) - Decimal(canonical["plan"]["stop"]))
        if risk == 0:
            raise RuntimeError("flat result has zero Entry/Stop risk")
        return {
            "result": result,
            "canonical": canonical,
            "state": canonical["outcome"]["armed_state"],
            "symbol": canonical["symbol"],
            "direction": canonical["direction"],
            "trade_id": result.p7_summary.trade_id,
            "exit_time": canonical["outcome"].get("exit_time") or "",
            "gross_r": result.gross_r,
            "fees_r": result.fees / risk,
            "slippage_r": result.slippage / risk,
            "funding_r": result.funding / risk,
            "net_r": result.net_r,
            "mae_r": result.mae_r,
            "mfe_r": result.mfe_r,
            "fees_quote": result.fees,
            "slippage_quote": result.slippage,
            "funding_quote": result.funding,
            "risk_quote": risk,
            "result_hash": result.result_hash,
        }

    summary_module._trade_row = safe_trade_row


def _case_rows(directory: Path) -> dict[str, dict[str, Any]]:
    rows = {}
    for path in directory.glob("cases/*.json"):
        value = _load_json(path)
        canonical = json.loads(value["p7_summary"]["canonical_json"])
        rows[canonical["logical_signal_id"]] = {
            "state": canonical["outcome"]["armed_state"],
            "entered": canonical["outcome"]["armed_state"] == "TRIGGERED",
            "entry_time": canonical["outcome"].get("entry_time"),
            "exit_time": canonical["outcome"].get("exit_time"),
            "exit_reason": value.get("exit_reason"),
            "net_r": value.get("net_r"),
            "gross_r": value.get("gross_r"),
            "plan": canonical["plan"],
            "symbol": canonical["symbol"],
        }
    return rows


def _baseline_rows(scenario: CostScenario) -> dict[str, dict[str, Any]]:
    return _case_rows(BASELINE_REPLAY_DIRS[scenario])


def _metrics(summary: dict[str, Any]) -> dict[str, Any]:
    status = summary["status"]
    portfolio = summary["portfolio"]
    return {
        "entered": status["entered_trade_count"],
        "expired": status["outcome_state_counts"].get("EXPIRED", 0),
        "invalidated": status["outcome_state_counts"].get("INVALIDATED", 0),
        "exit_distribution": summary["exit_reasons"],
        "gross_r": portfolio["gross_r"],
        "fees_r": portfolio["fees_r"],
        "slippage_r": portfolio["slippage_r"],
        "funding_r": portfolio["funding_r"],
        "net_r": portfolio["net_r"],
        "avg_net_r": portfolio["average_net_r"],
        "median_net_r": portfolio["median_net_r"],
        "win_rate": portfolio["win_rate"],
        "profit_factor": portfolio["profit_factor"],
        "exit_order_dd": portfolio["realized_exit_order_max_drawdown_r"],
    }


def _baseline_delta(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    candidate_portfolio = candidate["portfolio"]
    baseline_portfolio = baseline["portfolio"]
    fields = (
        "entered_trade_count",
        "gross_r",
        "fees_r",
        "slippage_r",
        "funding_r",
        "net_r",
        "average_net_r",
        "median_net_r",
        "win_rate",
        "profit_factor",
        "realized_exit_order_max_drawdown_r",
    )
    result: dict[str, Any] = {}
    for field in fields:
        left, right = candidate_portfolio.get(field), baseline_portfolio.get(field)
        if left is None or right is None:
            result[field] = None
        else:
            result[field] = str(Decimal(str(left)) - Decimal(str(right)))
    result["expired"] = candidate["status"]["outcome_state_counts"].get("EXPIRED", 0) - baseline[
        "status"
    ]["outcome_state_counts"].get("EXPIRED", 0)
    result["invalidated"] = candidate["status"]["outcome_state_counts"].get(
        "INVALIDATED", 0
    ) - baseline["status"]["outcome_state_counts"].get("INVALIDATED", 0)
    return result


def _trigger_map(docs: tuple[dict[str, Any], ...]) -> dict[str, str]:
    return {
        item["logical_signal_id"]: item["primary_trigger"]
        for doc in docs
        for item in doc["confirmed_triggers"]
        if item.get("baseline_p6_status") == "ACCEPTED_PLAN"
    }


def _family_breakdown(
    artifact: StageAFunnelArtifact,
    docs: tuple[dict[str, Any], ...],
    candidate_rows: dict[CostScenario, dict[str, dict[str, Any]]],
    baseline_rows: dict[CostScenario, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    accepted_ids = set(artifact.accepted_logical_signal_ids)
    triggers = _trigger_map(docs)
    result: dict[str, Any] = {}
    stage_summary = _load_json(STAGE_A_SUMMARY)
    candidate = next(
        item
        for item in stage_summary["candidates"]
        if item["candidate_hash"] == artifact.candidate_hash
    )
    family_counts = candidate["structure_diagnostics"]["trigger_type_counts"]
    for family in ("MA_RECLAIM", "SWEEP_RECLAIM"):
        ids = {key for key in accepted_ids if triggers.get(key) == family}
        result[family] = {
            "stage_a_confirmed": family_counts.get(family, 0),
            "accepted": len(ids),
        }
        for scenario in CostScenario:
            rows = candidate_rows[scenario]
            values = [
                Decimal(str(rows[key]["net_r"]))
                for key in ids
                if rows.get(key, {}).get("net_r") is not None
            ]
            result[family][scenario.value] = {
                "entered": sum(bool(rows.get(key, {}).get("entered")) for key in ids),
                "net_r": str(sum(values, Decimal(0))),
            }
    return result


def _decomposition(
    candidate_hash: str,
    accepted_ids: set[str],
    baseline_ids: set[str],
    candidate_rows: dict[CostScenario, dict[str, dict[str, Any]]],
    baseline_rows: dict[CostScenario, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    common, newly, removed = (
        accepted_ids & baseline_ids,
        accepted_ids - baseline_ids,
        baseline_ids - accepted_ids,
    )
    common_rows = []
    for logical_id in sorted(common):
        item: dict[str, Any] = {"logical_signal_id": logical_id}
        for scenario in CostScenario:
            left, right = (
                baseline_rows[scenario].get(logical_id),
                candidate_rows[scenario].get(logical_id),
            )
            if left is None or right is None:
                continue
            item[scenario.value] = {
                "baseline": {
                    "plan": left["plan"],
                    "state": left["state"],
                    "entered": left["entered"],
                    "exit_reason": left["exit_reason"],
                    "net_r": left["net_r"],
                },
                "candidate": {
                    "plan": right["plan"],
                    "state": right["state"],
                    "entered": right["entered"],
                    "exit_reason": right["exit_reason"],
                    "net_r": right["net_r"],
                },
                "net_r_delta": str(
                    Decimal(str(right["net_r"] or 0)) - Decimal(str(left["net_r"] or 0))
                ),
            }
        common_rows.append(item)

    def group(ids: set[str], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
        values = [
            Decimal(str(rows[key]["net_r"]))
            for key in ids
            if rows.get(key, {}).get("net_r") is not None
        ]
        return {
            "signal_count": len(ids),
            "entered": sum(bool(rows.get(key, {}).get("entered")) for key in ids),
            "not_entered": sum(not bool(rows.get(key, {}).get("entered")) for key in ids),
            "net_r": str(sum(values, Decimal(0))),
        }

    return {
        "common_count": len(common),
        "new_count": len(newly),
        "removed_count": len(removed),
        "common_signals": common_rows,
        "newly_accepted": {
            scenario.value: group(newly, candidate_rows[scenario]) for scenario in CostScenario
        },
        "removed_baseline": {
            scenario.value: group(removed, baseline_rows[scenario]) for scenario in CostScenario
        },
        "common_aggregate": {
            scenario.value: {
                "candidate": group(common, candidate_rows[scenario]),
                "baseline": group(common, baseline_rows[scenario]),
                "net_r_delta": str(
                    Decimal(group(common, candidate_rows[scenario])["net_r"])
                    - Decimal(group(common, baseline_rows[scenario])["net_r"])
                ),
            }
            for scenario in CostScenario
        },
        "candidate_hash": candidate_hash,
    }


def _execute_pivot(
    artifact: StageAFunnelArtifact,
    report: DevSourceScanReport,
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    all_parameters: tuple[DevParameterVersion, ...],
    sources: tuple[Any, ...],
    baseline_ids: set[str],
    baseline_trigger_map: dict[str, str],
) -> dict[str, Any]:
    accepted = tuple(AcceptedP6Request.model_validate(item) for item in report.accepted_requests)
    requirements = P1.build_market_data_requirement_plan(report)
    transport = P1.build_binance_transport_plan(
        report=report, requirements=requirements, max_attempts=3
    )
    candidate_dir = OUT_ROOT / artifact.candidate_hash[:12]
    candidate_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        candidate_dir / f"{report.report_hash}.candidate-source-scan.json",
        report.model_dump(mode="json"),
    )
    _write_json(
        candidate_dir / f"{requirements.plan_hash}.requirements.json",
        requirements.model_dump(mode="json"),
    )
    _write_json(
        candidate_dir / f"{transport.transport_plan_hash}.transport-plan.json",
        transport.model_dump(mode="json"),
    )
    _write_json(candidate_dir / "stage_a_artifact.json", artifact.model_dump(mode="json"))
    (
        base_candles,
        base_funding,
        base_candle_intervals,
        base_funding_intervals,
        base_guard_rows,
        base_observed,
        base_supplements,
    ) = sources
    incremental, incremental_tasks = P1._incremental_plan(
        report,
        requirements,
        transport,
        base_candle_intervals,
        base_funding_intervals,
        base_funding,
        base_guard_rows,
        parameter.candidate.parameters.max_holding_bars,
    )
    transport_for_run = incremental
    retry_count = 0
    if incremental is not None:
        _write_json(
            candidate_dir / f"{incremental.transport_plan_hash}.incremental-transport-plan.json",
            incremental.model_dump(mode="json"),
        )
        checkpoint_path = (
            ROOT
            / "artifacts/market_data/transport_runs"
            / incremental.transport_plan_hash
            / "checkpoint.json"
        )
        if checkpoint_path.exists():
            transport_checkpoint = _load_json(checkpoint_path)
            if any(
                item["status"] in {"FAILED", "EXHAUSTED"}
                for item in transport_checkpoint["entries"]
            ):
                transport_for_run = _retry_transport_plan(incremental)
                retry_count = 1
                _write_json(
                    candidate_dir
                    / f"{transport_for_run.transport_plan_hash}.retry-transport-plan.json",
                    transport_for_run.model_dump(mode="json"),
                )
        with httpx.Client(verify=False, timeout=60.0) as client:
            run_binance_transport(
                project_dir=ROOT,
                data_dir=ROOT / "artifacts/market_data",
                plan=transport_for_run,
                approved_requirements_plan_hash=requirements.plan_hash,
                client=client,
            )
    inc_candles, inc_funding, inc_observed = P1._load_incremental_rows(transport_for_run)
    market_data, schedules = P1._candidate_market_data(
        candidate_dir=candidate_dir,
        report=report,
        requirements=requirements,
        transport=transport,
        holding=parameter.candidate.parameters.max_holding_bars,
        base_candles=base_candles,
        base_funding=base_funding,
        base_observed=base_observed,
        incremental_candles=inc_candles,
        incremental_funding=inc_funding,
        incremental_observed=inc_observed,
    )
    supplements = P1._candidate_supplements(
        candidate_dir=candidate_dir,
        report=report,
        artifacts=market_data,
        baseline_supplements=base_supplements,
    )
    prep: ReplayPrepResult = prepare_dev_replay(
        project_dir=ROOT,
        report=report,
        market_data=market_data,
        funding_schedules=schedules,
        funding_mark_supplements=supplements,
        requirements=requirements,
        transport_plan=transport,
    )
    prep_dir = candidate_dir / "replay_prep"
    prep_paths = write_replay_prep_artifacts(prep, prep_dir)
    _write_json(prep_dir / "summary.json", prep.summary.model_dump(mode="json"))
    _write_json(prep_dir / "manifest.json", prep.manifest.model_dump(mode="json"))
    supplement_run, uncertainty = P1._write_candidate_supplement_indexes(
        candidate_dir=candidate_dir,
        report=report,
        requirements=requirements,
        transport=transport,
        supplements=supplements,
    )
    if not prep.summary.can_start_dev_replay:
        raise RuntimeError("pivot ReplayPrep is blocked")
    summary_module = P1._load_summary_module()
    _patch_flat_trade_rows(summary_module)
    scenario_summaries: dict[str, dict[str, Any]] = {}
    rows: dict[CostScenario, dict[str, dict[str, Any]]] = {}
    for scenario in CostScenario:
        result_dir = candidate_dir / f"results_{scenario.value}"
        checkpoint_path = result_dir / "checkpoint.json"
        replay_checkpoint = run_replay_batch(
            project_dir=ROOT,
            manifest=prep.manifest,
            plan=plan,
            parameter_versions=all_parameters,
            market_data=market_data,
            funding_mark_supplements=supplements,
            results_dir=result_dir,
            checkpoint_path=checkpoint_path,
            candidate_hashes=(artifact.candidate_hash,),
            cost_scenarios=(scenario,),
        )
        if replay_checkpoint.status != "COMPLETE":
            raise RuntimeError(f"pivot {scenario.value} replay did not complete")
        summary = summary_module.build_summary(
            results_dir=result_dir,
            checkpoint_path=checkpoint_path,
            readiness_hash=prep.summary.summary_hash,
            supplement_run_path=supplement_run,
            uncertainty_path=uncertainty,
            supplement_dir=candidate_dir / "funding_mark_supplement",
        )
        _write_json(result_dir / f"{summary['summary_hash']}.summary.json", summary)
        scenario_summaries[scenario.value] = summary
        rows[scenario] = _case_rows(result_dir)
    baseline_rows = {scenario: _baseline_rows(scenario) for scenario in CostScenario}
    baseline_summaries = {
        scenario: _load_json(BASELINE_REPLAY_DIRS[scenario] / "summary.json")
        for scenario in CostScenario
    }
    new_candle_rows = sum(len(values) for values in inc_candles.values())
    new_funding_rows = sum(len(values) for values in inc_funding.values())
    required_funding_rows = sum(len(item.settlement_times) for item in schedules)
    return {
        "candidate_hash": artifact.candidate_hash,
        "stage_a_scanner_report_hash": artifact.scanner_report_hash,
        "reconstructed_report_hash": report.report_hash,
        "report_hash_matches_stage_a": report.report_hash == artifact.scanner_report_hash,
        "parameter_content_hash": parameter.content_hash,
        "stage_a_artifact_hash": artifact.artifact_hash,
        "accepted_requests": len(accepted),
        "requirements_hash": requirements.plan_hash,
        "requirements_windows": sum(len(item.windows) for item in requirements.requirements),
        "requirements_one_minute_rows": requirements.one_minute_rows_after_overlap_dedup,
        "transport_plan_hash": transport.transport_plan_hash,
        "incremental_transport_plan_hash": incremental.transport_plan_hash if incremental else None,
        "retry_transport_plan_hash": (
            transport_for_run.transport_plan_hash
            if transport_for_run is not None and retry_count
            else None
        ),
        "retry_count": retry_count,
        "incremental_tasks": incremental_tasks,
        "network_accessed": incremental is not None,
        "reused_candle_rows": max(
            0, requirements.one_minute_rows_after_overlap_dedup - new_candle_rows
        ),
        "new_candle_rows": new_candle_rows,
        "reused_funding_rows": max(0, required_funding_rows - new_funding_rows),
        "new_funding_rows": new_funding_rows,
        "new_guard_rows": sum(
            item["role"] in {"FUNDING_GUARD_BEFORE", "FUNDING_GUARD_AFTER"}
            for item in incremental_tasks
        ),
        "replay_ready": prep.summary.replay_ready_count,
        "blocked": prep.summary.not_ready_request_count,
        "readiness_hash": prep.summary.summary_hash,
        "supplement_hashes": [item.artifact_hash for item in supplements],
        "supplement_count": len(supplements),
        "warnings": sorted({item.warning for item in supplements}),
        "readiness_issues": [item.model_dump(mode="json") for item in prep.summary.issues],
        "scenario_summaries": scenario_summaries,
        "metrics": {
            scenario.value: _metrics(scenario_summaries[scenario.value])
            for scenario in CostScenario
        },
        "baseline_deltas": {
            scenario.value: _baseline_delta(
                scenario_summaries[scenario.value], baseline_summaries[scenario]
            )
            for scenario in CostScenario
        },
        "decomposition": _decomposition(
            artifact.candidate_hash,
            set(item.request.request.armed.logical_signal_id for item in accepted),
            baseline_ids,
            rows,
            baseline_rows,
        ),
        "trigger_family": _family_breakdown(
            artifact, _checkpoint_docs(artifact), rows, baseline_rows
        ),
        "prep_paths": [str(item) for item in prep_paths],
        "baseline_trigger_map": baseline_trigger_map,
    }


def _baseline_result_references() -> dict[str, Any]:
    return {
        scenario.value: {
            "summary_hash": _load_json(BASELINE_REPLAY_DIRS[scenario] / "summary.json")[
                "summary_hash"
            ],
            "summary_path": (BASELINE_REPLAY_DIRS[scenario] / "summary.json")
            .relative_to(ROOT)
            .as_posix(),
            "replay_executed_here": False,
        }
        for scenario in CostScenario
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--semantic-only",
        action="store_true",
        help="reconstruct Stage A requests and run only the compression gate",
    )
    args = parser.parse_args(argv)
    summary = _load_json(STAGE_A_SUMMARY)
    if summary.get("summary_hash") != STAGE_A_SUMMARY.stem:
        raise RuntimeError("Stage A summary hash mismatch")
    plan = SensitivityPlan.model_validate_json(
        (ROOT / "data/manifests/sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    baseline_report = DevSourceScanReport.model_validate_json(
        (ROOT / "data/manifests/dev_source_scan" / f"{BASE_REPORT_HASH}.json").read_bytes()
    )
    baseline_artifact = _load_stage_a(BASELINE_CANDIDATE_HASH)
    baseline_docs = _checkpoint_docs(baseline_artifact)
    baseline_accepted = tuple(
        AcceptedP6Request.model_validate(item) for item in baseline_report.accepted_requests
    )
    baseline_ids = {item.request.request.armed.logical_signal_id for item in baseline_accepted}
    baseline_trigger_map = _trigger_map(baseline_docs)
    versions = {
        version.candidate.candidate_hash: version
        for path in (ROOT / "data/manifests/parameter_version").glob("*.json")
        for version in [DevParameterVersion.model_validate_json(path.read_bytes())]
        if version.sensitivity_plan_hash == PLAN_HASH
    }
    all_parameters = tuple(sorted(versions.values(), key=lambda item: item.content_hash))
    sources = _merge_sources()
    compression: dict[str, Any] = {}
    pivot_result: dict[str, Any] | None = None
    for name, candidate_hash in CANDIDATES.items():
        artifact = _load_stage_a(candidate_hash)
        docs = _checkpoint_docs(artifact)
        accepted = _accepted_from_checkpoints(docs)
        report = _report_from_checkpoints(artifact, docs, baseline_report)
        if name.startswith("compression"):
            gate = _semantic_gate(baseline_accepted, accepted)
            compression[name] = {
                **gate,
                "candidate_hash": candidate_hash,
                "stage_a_scanner_report_hash": artifact.scanner_report_hash,
                "report_hash_matches_stage_a": report.report_hash == artifact.scanner_report_hash,
                "accepted_request_count": len(accepted),
                "status": "REPLAY_EQUIVALENT_TO_BASELINE"
                if gate["replay_semantic_equivalence"]
                else "FULL_STAGE_B_REQUIRED",
                "replay_executed_here": False,
                "baseline_results": _baseline_result_references(),
                "candidate_report_hash": report.report_hash,
            }
    if args.semantic_only:
        print(json.dumps(compression, ensure_ascii=False, sort_keys=True))
        return 0
    for name, candidate_hash in CANDIDATES.items():
        if name != "pivot_window_3x3":
            continue
        artifact = _load_stage_a(candidate_hash)
        docs = _checkpoint_docs(artifact)
        report = _report_from_checkpoints(artifact, docs, baseline_report)
        pivot_result = _execute_pivot(
            artifact,
            report,
            plan,
            versions[candidate_hash],
            all_parameters,
            sources,
            baseline_ids,
            baseline_trigger_map,
        )
    payload = {
        "schema_version": "p9-phase3-stage-b-summary/0.1.0",
        "phase": "Phase 3 Stage B",
        "stage_a_summary_hash": summary["summary_hash"],
        "stage_b_authorized": True,
        "validation_consumed": False,
        "locked_test_consumed": False,
        "baseline_candidate_hash": BASELINE_CANDIDATE_HASH,
        "baseline_results": _baseline_result_references(),
        "compression_semantic_equivalence": compression,
        "pivot_window_3x3": pivot_result,
    }
    final = {**payload, "summary_hash": _hash(payload)}
    _write_json(OUT_ROOT / f"{final['summary_hash']}.json", final)
    pointer = OUT_ROOT / "phase3-stage-b-summary.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_bytes(canonical_json_bytes(final))
    print(
        json.dumps(
            {
                "summary_hash": final["summary_hash"],
                "path": str(OUT_ROOT / f"{final['summary_hash']}.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
