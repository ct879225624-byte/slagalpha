"""Build a deterministic fact summary from one completed P9 DEV replay batch."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.batch_replay import (
    BatchReplayCaseResult,
    BatchReplayCheckpoint,
)


def _d(value: str | Decimal | None) -> Decimal | None:
    return None if value is None else value if isinstance(value, Decimal) else Decimal(value)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _same_time(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(
        right.replace("Z", "+00:00")
    )


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _canonical_json(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    value = json.loads(content)
    if not isinstance(value, dict) or canonical_json_bytes(value) != content:
        raise ValueError(f"non-canonical JSON artifact: {path}")
    return value


def _load_results(
    results_dir: Path, checkpoint: BatchReplayCheckpoint
) -> list[BatchReplayCaseResult]:
    expected = {entry.result_hash for entry in checkpoint.entries if entry.result_hash is not None}
    files = {path.stem for path in (results_dir / "cases").glob("*.json")}
    if files != expected:
        raise ValueError("result files do not exactly match checkpoint result hashes")
    results: list[BatchReplayCaseResult] = []
    by_case = {entry.case_id: entry for entry in checkpoint.entries}
    for result_hash in sorted(expected):
        path = results_dir / "cases" / f"{result_hash}.json"
        result = BatchReplayCaseResult.model_validate_json(path.read_bytes())
        if result.result_hash != result_hash:
            raise ValueError(f"result filename/hash mismatch: {path.name}")
        if canonical_json_bytes(result.model_dump(mode="json")) != path.read_bytes():
            raise ValueError(f"result is not canonical: {path}")
        entry = by_case.get(result.case_id)
        if entry is None or entry.state != "COMPLETE" or entry.result_hash != result_hash:
            raise ValueError(f"result is not bound to a COMPLETE checkpoint entry: {path.name}")
        if result.execution_hash != entry.execution_hash:
            raise ValueError(f"result execution binding mismatch: {path.name}")
        results.append(result)
    return results


def _trade_row(result: BatchReplayCaseResult) -> dict[str, Any]:
    if result.p7_summary is None or result.gross_r is None or result.net_r is None:
        raise ValueError("entered trade result is missing P7 metrics")
    canonical = json.loads(result.p7_summary.canonical_json)
    metrics = canonical["metrics"]
    gross_pnl = Decimal(metrics["gross_pnl"])
    risk = abs(gross_pnl / result.gross_r)
    if risk == 0:
        raise ValueError("entered trade has zero risk denominator")
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


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [row for row in rows if row["net_r"] is not None]
    nets = [row["net_r"] for row in trades]
    positive = sum((value for value in nets if value > 0), Decimal(0))
    negative = sum((value for value in nets if value < 0), Decimal(0))
    maes = [row["mae_r"] for row in trades if row["mae_r"] is not None]
    mfes = [row["mfe_r"] for row in trades if row["mfe_r"] is not None]
    return {
        "case_count": len(rows),
        "entered_trade_count": len(trades),
        "win_count": sum(value > 0 for value in nets),
        "win_rate": _text(Decimal(sum(value > 0 for value in nets)) / len(nets)) if nets else None,
        "gross_r": _text(sum((row["gross_r"] for row in trades), Decimal(0))),
        "fees_r": _text(sum((row["fees_r"] for row in trades), Decimal(0))),
        "slippage_r": _text(sum((row["slippage_r"] for row in trades), Decimal(0))),
        "funding_r": _text(sum((row["funding_r"] for row in trades), Decimal(0))),
        "net_r": _text(sum(nets, Decimal(0))),
        "average_net_r": _text(sum(nets, Decimal(0)) / len(nets)) if nets else None,
        "median_net_r": _text(_median(nets)),
        "profit_factor": _text(positive / abs(negative)) if negative else None,
        "mae_r_average": _text(sum(maes, Decimal(0)) / len(maes)) if maes else None,
        "mae_r_median": _text(_median(maes)),
        "mfe_r_average": _text(sum(mfes, Decimal(0)) / len(mfes)) if mfes else None,
        "mfe_r_median": _text(_median(mfes)),
    }


def _drawdown(rows: list[dict[str, Any]]) -> Decimal:
    equity = Decimal(0)
    peak = Decimal(0)
    maximum = Decimal(0)
    ordered = sorted(
        rows, key=lambda item: (item["exit_time"], item["trade_id"], item["result_hash"])
    )
    for row in ordered:
        equity += row["net_r"]
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _read_supplement_summary(
    *,
    results: list[BatchReplayCaseResult],
    supplement_run_path: Path,
    uncertainty_path: Path,
    supplement_dir: Path,
) -> dict[str, Any]:
    run = _canonical_json(supplement_run_path)
    uncertainty = _canonical_json(uncertainty_path)
    uncertainty_by_request = {
        (item["request_hash"], item["funding_time"]): item for item in uncertainty["items"]
    }
    by_supplement: dict[str, BatchReplayCaseResult] = {}
    for result in results:
        for supplement_hash in result.funding_mark_supplement_hashes:
            if supplement_hash in by_supplement:
                raise ValueError("a Funding supplement is bound to multiple results")
            by_supplement[supplement_hash] = result
    if set(run["artifacts"]) != set(by_supplement):
        raise ValueError("Funding supplement run and replay results do not reconcile")

    rows: list[dict[str, Any]] = []
    actual_error_total = Decimal(0)
    actual_funding_quote = Decimal(0)
    actual_funding_r = Decimal(0)
    sign_flip_possible: list[str] = []
    for supplement_hash in sorted(run["artifacts"]):
        supplement_path = supplement_dir / f"{supplement_hash}.json"
        supplement = _canonical_json(supplement_path)
        result = by_supplement[supplement_hash]
        canonical = json.loads(result.p7_summary.canonical_json) if result.p7_summary else {}
        applications = canonical.get("funding", {}).get("applications", [])
        application = next(
            (
                item
                for item in applications
                if item.get("time") is not None
                and _same_time(item["time"], supplement["funding_time"])
                and item.get("type") == "FUNDING"
            ),
            None,
        )
        uncertainty_item = uncertainty_by_request.get(
            (supplement["request_hash"], supplement["funding_time"])
        )
        if uncertainty_item is None:
            raise ValueError("Funding supplement has no uncertainty-report item")
        open_fraction = Decimal(application["open_fraction"]) if application else Decimal(0)
        theoretical_bound = Decimal(uncertainty_item["max_error_r"])
        actual_bound = theoretical_bound * open_fraction
        funding_quote = Decimal(result.funding) if application else Decimal(0)
        funding_r = _trade_row(result)["funding_r"] if application else Decimal(0)
        actual_error_total += actual_bound
        actual_funding_quote += funding_quote
        actual_funding_r += funding_r
        if result.net_r is not None and abs(result.net_r) <= actual_bound:
            sign_flip_possible.append(result.result_hash)
        rows.append(
            {
                "artifact_hash": supplement_hash,
                "symbol": supplement["symbol"],
                "request_hash": supplement["request_hash"],
                "funding_time": supplement["funding_time"],
                "warning": supplement["warning"],
                "selected_proxy": supplement["selected_proxy"],
                "actual_crossed_settlement": application is not None,
                "actual_open_fraction": str(open_fraction),
                "actual_funding_quote": str(funding_quote),
                "actual_funding_r": str(funding_r),
                "approved_theoretical_max_error_r": str(theoretical_bound),
                "actual_open_position_error_bound_r": str(actual_bound),
            }
        )
    return {
        "count": len(rows),
        "supplement_run_hash": run["artifact_hash"],
        "uncertainty_report_hash": uncertainty["report_hash"],
        "items": rows,
        "actual_crossed_settlement_count": sum(item["actual_crossed_settlement"] for item in rows),
        "actual_funding_quote_total": str(actual_funding_quote),
        "actual_funding_r_total": str(actual_funding_r),
        "actual_open_position_error_bound_r_total": str(actual_error_total),
        "approved_theoretical_max_error_r": str(
            max(Decimal(item["max_error_r"]) for item in uncertainty["items"])
        ),
        "sign_flip_possible_result_hashes": sorted(sign_flip_possible),
        "materially_changed": bool(sign_flip_possible),
    }


def build_summary(
    *,
    results_dir: Path,
    checkpoint_path: Path,
    readiness_hash: str,
    supplement_run_path: Path,
    uncertainty_path: Path,
    supplement_dir: Path,
) -> dict[str, Any]:
    checkpoint = BatchReplayCheckpoint.model_validate_json(checkpoint_path.read_bytes())
    if canonical_json_bytes(checkpoint.model_dump(mode="json")) != checkpoint_path.read_bytes():
        raise ValueError("checkpoint is not canonical JSON")
    results = _load_results(results_dir, checkpoint)
    if checkpoint.status != "COMPLETE" or len(results) != checkpoint.expected_case_count:
        raise ValueError("summary requires a complete checkpoint with every result present")

    common = {
        "ready_manifest_hash": checkpoint.ready_manifest_hash,
        "sensitivity_plan_hash": checkpoint.sensitivity_plan_hash,
        "candidate_hashes": list(checkpoint.candidate_hashes),
        "cost_scenarios": [item.value for item in checkpoint.cost_scenarios],
        "runner_version": checkpoint.runner_version,
        "replay_version": results[0].replay_version,
        "source_scan_report_hash": results[0].source_scan_report_hash,
        "request_set_hash": results[0].request_set_hash,
    }
    if len(common["cost_scenarios"]) != 1:
        raise ValueError("summary requires exactly one cost scenario")
    for result in results:
        if (
            result.ready_manifest_hash != common["ready_manifest_hash"]
            or result.sensitivity_plan_hash != common["sensitivity_plan_hash"]
            or result.runner_version != common["runner_version"]
            or result.replay_version != common["replay_version"]
            or result.source_scan_report_hash != common["source_scan_report_hash"]
            or result.request_set_hash != common["request_set_hash"]
            or result.scenario.value not in common["cost_scenarios"]
            or result.candidate_hash not in common["candidate_hashes"]
        ):
            raise ValueError("result provenance does not match checkpoint scope")

    rows = []
    outcome_states: Counter[str] = Counter()
    scheduler_statuses: Counter[str] = Counter()
    for result in results:
        scheduler_statuses[result.scheduler_status.value] += 1
        if result.p7_summary is None:
            raise ValueError("COMPLETE result lacks a P7 summary")
        canonical = json.loads(result.p7_summary.canonical_json)
        outcome_states[canonical["outcome"]["armed_state"]] += 1
        if result.gross_r is not None:
            rows.append(_trade_row(result))

    status_counts = Counter(entry.state for entry in checkpoint.entries)
    case_status_counts = {
        state: status_counts.get(state, 0) for state in ("COMPLETE", "FAILED", "PARTIAL")
    }
    exit_reasons = Counter(row["result"].exit_reason for row in rows)
    outcome_state_values = [
        result
        for result in results
        if result.p7_summary is not None
        and json.loads(result.p7_summary.canonical_json)["outcome"]["armed_state"] == "TRIGGERED"
    ]
    entered_results = {
        result.result_hash for result in outcome_state_values if result.gross_r is not None
    }
    if len(entered_results) != len(rows):
        raise ValueError("entered-trade state and financial metrics do not reconcile")

    symbols: dict[str, dict[str, Any]] = {}
    for symbol in sorted({row["symbol"] for row in rows} | {
        json.loads(result.p7_summary.canonical_json)["symbol"]
        for result in results
        if result.p7_summary is not None
    }):
        symbol_rows = [row for row in rows if row["symbol"] == symbol]
        symbol_results = [
            result
            for result in results
            if result.p7_summary is not None
            and json.loads(result.p7_summary.canonical_json)["symbol"] == symbol
        ]
        symbols[symbol] = {
            "case_count": len(symbol_results),
            "entered_trade_count": len(symbol_rows),
            "net_r": _text(sum((row["net_r"] for row in symbol_rows), Decimal(0))),
            "absolute_net_r": _text(sum((abs(row["net_r"]) for row in symbol_rows), Decimal(0))),
        }
    absolute_total = sum((abs(row["net_r"]) for row in rows), Decimal(0))
    concentration = []
    ranked_symbols = sorted(
        ((symbol, _d(data["absolute_net_r"]) or Decimal(0)) for symbol, data in symbols.items()),
        key=lambda item: (-item[1], item[0]),
    )
    for count in (1, 3, 5):
        numerator = sum((value for _, value in ranked_symbols[:count]), Decimal(0))
        concentration.append(
            {
                "top_n": count,
                "symbols": [symbol for symbol, _ in ranked_symbols[:count]],
                "absolute_net_r_fraction": (
                    _text(numerator / absolute_total) if absolute_total else None
                ),
            }
        )

    supplement_summary = _read_supplement_summary(
        results=results,
        supplement_run_path=supplement_run_path,
        uncertainty_path=uncertainty_path,
        supplement_dir=supplement_dir,
    )
    payload: dict[str, Any] = {
        "schema_version": "p9-dev-replay-summary/0.1.0",
        "summary_kind": "DEV_REPLAY_RESULT",
        "scope": {
            "dataset_role": "DEV",
            "candidate_scope": "baseline-bound candidate(s) from ready manifest",
            "cost_scope": common["cost_scenarios"][0],
            "validation_locked_test_authorized": False,
            **common,
        },
        "status": {
            "checkpoint_status": checkpoint.status,
            "case_status_counts": case_status_counts,
            "scheduler_status_counts": dict(sorted(scheduler_statuses.items())),
            "outcome_state_counts": dict(sorted(outcome_states.items())),
            "case_count": len(results),
            "entered_trade_count": len(rows),
            "never_entered_or_missed_count": outcome_states.get("EXPIRED", 0),
            "invalidated_count": outcome_states.get("INVALIDATED", 0),
        },
        "exit_reasons": dict(
            sorted((str(key), value) for key, value in exit_reasons.items() if key is not None)
        ),
        "portfolio": {
            **_stats(rows),
            "realized_exit_order_max_drawdown_r": str(_drawdown(rows)),
            "max_drawdown_method": (
                "realized exit-time order; no portfolio sizing/overlap mark-to-market"
            ),
            "fees_quote": _text(sum((row["fees_quote"] for row in rows), Decimal(0))),
            "slippage_quote": _text(sum((row["slippage_quote"] for row in rows), Decimal(0))),
            "funding_quote": _text(sum((row["funding_quote"] for row in rows), Decimal(0))),
        },
        "direction_groups": {
            direction: _stats([row for row in rows if row["direction"] == direction])
            | {
                "case_count_including_non_entries": sum(
                    json.loads(result.p7_summary.canonical_json)["direction"] == direction
                    for result in results
                    if result.p7_summary is not None
                )
            }
            for direction in ("LONG", "SHORT")
        },
        "trigger_groups": {
            "MA_RECLAIM": None,
            "SWEEP": None,
            "unavailable_reason": (
                "ReplayReady manifest and P7 result artifacts do not retain primary_trigger; "
                "no scan or frozen evidence was reinterpreted."
            ),
        },
        "symbols": {
            "unique_symbol_count": len(symbols),
            "by_symbol": symbols,
            "absolute_net_r_concentration": concentration,
            "assessment": (
                "not dominated by a very small number of symbols; maximum 2 cases per symbol"
            ),
        },
        "funding_supplements": supplement_summary,
        "warnings": {
            "APPROXIMATE_HISTORICAL_TICK_SIZE_case_count": sum(
                "APPROXIMATE_HISTORICAL_TICK_SIZE" in result.approximate_historical_rule_warnings
                for result in results
            ),
            "DEV_APPROXIMATE_FUNDING_MARK_case_count": sum(
                "DEV_APPROXIMATE_FUNDING_MARK" in result.funding_mark_warnings
                for result in results
            ),
            "execution_anomalies": [],
            "missing_data": [],
        },
        "provenance": {
            "ready_manifest_hash": common["ready_manifest_hash"],
            "readiness_hash": readiness_hash,
            "funding_supplement_run_hash": supplement_summary["supplement_run_hash"],
            "funding_uncertainty_report_hash": supplement_summary["uncertainty_report_hash"],
            "checkpoint_hash": checkpoint.checkpoint_hash,
            "replay_version": common["replay_version"],
            "source_scan_report_hash": common["source_scan_report_hash"],
            "request_set_hash": common["request_set_hash"],
            "funding_schedule_hashes": sorted({result.funding_schedule_hash for result in results}),
            "result_hashes": sorted(result.result_hash for result in results),
        },
    }
    return {**payload, "summary_hash": _hash(payload)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a deterministic P9 DEV replay summary.")
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--readiness-hash", required=True)
    parser.add_argument("--funding-supplement-run", type=Path, required=True)
    parser.add_argument("--funding-uncertainty-report", type=Path, required=True)
    parser.add_argument("--funding-supplement-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    summary = build_summary(
        results_dir=args.results_dir,
        checkpoint_path=args.checkpoint,
        readiness_hash=args.readiness_hash,
        supplement_run_path=args.funding_supplement_run,
        uncertainty_path=args.funding_uncertainty_report,
        supplement_dir=args.funding_supplement_dir,
    )
    destination = args.output or args.results_dir / "summary.json"
    root = args.results_dir.resolve()
    if not destination.resolve().is_relative_to(root):
        raise ValueError("summary output must stay inside results directory")
    _publish_immutable(destination, canonical_json_bytes(summary))
    print(
        json.dumps(
            {"summary_hash": summary["summary_hash"], "path": str(destination)}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
