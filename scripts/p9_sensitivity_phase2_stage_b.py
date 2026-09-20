"""Execute the four approved P9 Phase 2 Stage-B candidates.

The Stage-A immutable request sets are the only candidate input.  This wrapper
reuses the existing Phase-1 requirements/transport/ReplayPrep helpers and the
formal P7 batch replay; it does not scan or alter strategy semantics.
"""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx

from slagalpha.backtest.costs import CostScenario
from slagalpha.data.binance_usdm_transport import (
    run_binance_transport,
)
from slagalpha.research.batch_replay import run_replay_batch
from slagalpha.research.dev_source_scan import (
    AcceptedP6Request,
    DevSourceScanReport,
)
from slagalpha.research.funding_mark_supplement import FundingMarkSupplementArtifact
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replan_sensitivity import (
    FINAL_LEDGER_HASH,
    SUPPORTED_CANDIDATES,
    ReplanCandidateArtifact,
)
from slagalpha.research.replay_market_data import ReplayMarketDataArtifact
from slagalpha.research.replay_prep import (
    ReplayPrepResult,
    prepare_dev_replay,
    write_replay_prep_artifacts,
)
from slagalpha.research.sensitivity import SensitivityPlan

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
BASE_REPORT_HASH = "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2"
STAGE_A_DIR = ROOT / "artifacts" / "sens_p2_replan_stage_a"
OUT_ROOT = ROOT / "artifacts" / "sens_p2_stage_b_final"
BASELINE_REPLAY_DIRS = {
    CostScenario.ZERO: ROOT / "artifacts" / "dev_replay_baseline_ZERO_20260913",
    CostScenario.BASELINE: ROOT / "artifacts" / "dev_replay_baseline_BASELINE_20260913",
    CostScenario.STRESS: ROOT / "artifacts" / "dev_replay_baseline_STRESS_20260913",
}


def _load_phase1() -> Any:
    path = ROOT / "scripts" / "p9_sensitivity_phase1.py"
    spec = importlib.util.spec_from_file_location("p9_sensitivity_phase1_stage_b", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Phase-1 orchestration helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P1 = _load_phase1()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    P1._write_json(path, value)


def _load_stage_a(candidate_hash: str) -> ReplanCandidateArtifact:
    matches = []
    for path in STAGE_A_DIR.glob("*.replan.json"):
        artifact = ReplanCandidateArtifact.model_validate_json(path.read_bytes())
        if artifact.candidate_hash == candidate_hash:
            matches.append((path, artifact))
    if len(matches) != 1:
        raise RuntimeError(f"expected one Stage-A artifact for {candidate_hash}")
    path, artifact = matches[0]
    if path.stem != artifact.artifact_hash + ".replan":
        raise RuntimeError("Stage-A artifact filename/hash mismatch")
    if artifact.status != "COMPLETE" or artifact.source_final_ledger_hash != FINAL_LEDGER_HASH:
        raise RuntimeError("Stage-A artifact is not a complete final-ledger result")
    if artifact.input_record_count != 2946:
        raise RuntimeError("Stage-A artifact does not contain all 2946 records")
    return artifact


def _accepted(artifact: ReplanCandidateArtifact) -> tuple[AcceptedP6Request, ...]:
    result = tuple(
        AcceptedP6Request.model_validate(item) for item in artifact.accepted_request_payloads
    )
    if len(result) != artifact.accepted_count:
        raise RuntimeError("Stage-A accepted payload count mismatch")
    return result


def _candidate_report(
    base_report: DevSourceScanReport,
    accepted: tuple[AcceptedP6Request, ...],
    parameter_hash: str,
    stage_a: ReplanCandidateArtifact,
) -> DevSourceScanReport:
    """Reuse the Phase-1 report envelope with the Stage-A P6 funnel."""

    grouped: dict[str, list[tuple[Any, Any, str]]] = {}
    for item in accepted:
        request = item.request
        grouped.setdefault(request.request.armed.symbol, []).append(
            (request.start, request.end_exclusive, request.request_hash)
        )
    requirement_rows = []
    for symbol in sorted(grouped):
        windows = P1._merged_windows(grouped[symbol])
        requirement_rows.append(
            {
                "symbol": symbol,
                "windows": [item.model_dump(mode="json") for item in windows],
                "one_minute_record_count": sum(item.one_minute_record_count for item in windows),
                "funding_record_count_estimate": None,
            }
        )
    market_requirement = P1.DevMarketDataRequirement(
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
    payload = base_report.model_dump(
        mode="json",
        exclude={
            "report_hash",
            "accepted_requests",
            "request_set_hash",
            "parameter_content_hash",
            "market_data_requirement",
        },
    )
    payload.update(
        {
            "parameter_content_hash": parameter_hash,
            "accepted_requests": [item.model_dump(mode="json") for item in accepted],
            "request_set_hash": P1._hash(
                {"accepted_requests": [item.model_dump(mode="json") for item in accepted]}
            ),
            "market_data_requirement": market_requirement.model_dump(mode="json"),
        }
    )
    funnel = dict(payload["funnel"])
    funnel.update(
        {
            "entry_stop_rejected_count": stage_a.entry_stop_rejected_count,
            "take_profit_rejected_count": stage_a.take_profit_rejected_count,
            "request_boundary_rejected_count": stage_a.request_boundary_rejected_count,
            "accepted_count": stage_a.accepted_count,
        }
    )
    payload["funnel"] = funnel
    payload["accepted_symbols"] = sorted({item.request.request.armed.symbol for item in accepted})
    return DevSourceScanReport.model_validate({**payload, "report_hash": P1._hash(payload)})


def _merge_sources() -> tuple[Any, ...]:
    candles, funding, candle_intervals, funding_intervals, guard_rows, observed, supplements = (
        P1._load_baseline_sources()
    )
    artifact_paths = sorted((ROOT / "artifacts" / "sens_p1").glob("**/market_data/*.json"))
    for path in artifact_paths:
        artifact = ReplayMarketDataArtifact.model_validate_json(path.read_bytes())
        for response in artifact.responses:
            body = json.loads((ROOT / response.relative_path).read_text(encoding="utf-8"))
            observed[response.symbol] = max(
                observed.get(response.symbol, response.observed_at), response.observed_at
            )
            left, right = response.start_time_ms, response.end_time_ms + 1
            if artifact.role == "CANDLE_ONE_MINUTE":
                candle_intervals.setdefault(response.symbol, []).append((left, right))
                for row in body:
                    candles[response.symbol][int(row[0])] = row
            else:
                funding_intervals.setdefault(response.symbol, []).append((left, right))
                for row in body:
                    funding[response.symbol][int(row["fundingTime"])] = row
    for symbol in list(candle_intervals):
        candle_intervals[symbol] = P1._interval_union(candle_intervals[symbol])
    for symbol in list(funding_intervals):
        funding_intervals[symbol] = P1._interval_union(funding_intervals[symbol])
    for path in sorted((ROOT / "artifacts" / "sens_p1").glob("**/funding_mark_supplement/*.json")):
        item = FundingMarkSupplementArtifact.model_validate_json(path.read_bytes())
        supplements[item.request_hash] = item
    return candles, funding, candle_intervals, funding_intervals, guard_rows, observed, supplements


def _trade_row_patch(summary_module: Any) -> None:
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
    rows: dict[str, dict[str, Any]] = {}
    for path in directory.glob("cases/*.json"):
        value = _load_json(path)
        canonical = json.loads(value["p7_summary"]["canonical_json"])
        logical_id = canonical["logical_signal_id"]
        rows[logical_id] = {
            "net_r": value["net_r"],
            "entered": canonical["outcome"]["armed_state"] == "TRIGGERED",
            "state": canonical["outcome"]["armed_state"],
            "entry_time": canonical["outcome"].get("entry_time"),
            "exit_reason": value.get("exit_reason"),
            "symbol": canonical["symbol"],
        }
    return rows


def _baseline_summary(scenario: CostScenario) -> dict[str, Any]:
    return _load_json(BASELINE_REPLAY_DIRS[scenario] / "summary.json")


def _special_breakdown(
    candidate_hash: str,
    scenario: CostScenario,
    candidate_rows: dict[str, dict[str, Any]],
    baseline_rows: dict[str, dict[str, Any]],
    accepted_ids: set[str],
    baseline_ids: set[str],
) -> dict[str, Any]:
    common = accepted_ids & baseline_ids
    newly = accepted_ids - baseline_ids
    removed = baseline_ids - accepted_ids

    def contribution(rows: dict[str, dict[str, Any]], ids: set[str]) -> dict[str, Any]:
        values = [
            Decimal(str(rows[item]["net_r"]))
            for item in ids
            if item in rows and rows[item]["net_r"] is not None
        ]
        return {
            "signal_count": len(ids),
            "entered_count": len(values),
            "net_r": str(sum(values, Decimal(0))),
        }

    return {
        "common": contribution(candidate_rows, common),
        "baseline_common": contribution(baseline_rows, common),
        "newly_accepted": contribution(candidate_rows, newly),
        "removed_from_candidate": contribution(baseline_rows, removed),
        "common_delta_net_r": str(
            Decimal(contribution(candidate_rows, common)["net_r"])
            - Decimal(contribution(baseline_rows, common)["net_r"])
        ),
        "candidate_hash": candidate_hash,
        "scenario": scenario.value,
    }


def _scenario_metrics(
    summary: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    baseline_summary: dict[str, Any],
    baseline_rows: dict[str, dict[str, Any]],
    zero_net: Decimal,
    baseline_zero_net: Decimal,
) -> dict[str, Any]:
    portfolio = summary["portfolio"]
    baseline_portfolio = baseline_summary["portfolio"]
    states: dict[str, int] = {}
    for row in rows.values():
        states[row["state"]] = states.get(row["state"], 0) + 1
    net = Decimal(str(portfolio["net_r"]))
    baseline_net = Decimal(str(baseline_portfolio["net_r"]))
    erosion = zero_net - net
    baseline_erosion = baseline_zero_net - baseline_net
    return {
        "entered": states.get("TRIGGERED", 0),
        "expired": states.get("EXPIRED", 0),
        "invalidated": states.get("INVALIDATED", 0),
        "exit_reason_distribution": summary["exit_reasons"],
        "gross_r": portfolio["gross_r"],
        "fees_r": portfolio["fees_r"],
        "slippage_r": portfolio["slippage_r"],
        "funding_r": portfolio["funding_r"],
        "net_r": portfolio["net_r"],
        "avg_net_r_per_entered_trade": portfolio["average_net_r"],
        "median_net_r": portfolio["median_net_r"],
        "win_rate": portfolio["win_rate"],
        "profit_factor": portfolio["profit_factor"],
        "exit_order_drawdown_r": portfolio["realized_exit_order_max_drawdown_r"],
        "delta_vs_baseline_holding_32": {
            "net_r": str(net - baseline_net),
            "profit_factor": str(
                Decimal(str(portfolio["profit_factor"]))
                - Decimal(str(baseline_portfolio["profit_factor"]))
            ),
            "entered": states.get("TRIGGERED", 0)
            - sum(item["state"] == "TRIGGERED" for item in baseline_rows.values()),
            "cost_erosion": str(erosion - baseline_erosion),
        },
    }


def _outcome_changes(
    candidate_rows: dict[str, dict[str, Any]],
    baseline_rows: dict[str, dict[str, Any]],
    common_ids: set[str],
) -> list[dict[str, Any]]:
    changed = []
    for logical_id in sorted(common_ids):
        candidate = candidate_rows.get(logical_id)
        baseline = baseline_rows.get(logical_id)
        if candidate is None or baseline is None:
            continue
        fields = ("state", "entry_time", "exit_reason")
        if any(candidate[field] != baseline[field] for field in fields):
            changed.append(
                {
                    "logical_signal_id": logical_id,
                    "baseline": {field: baseline[field] for field in fields},
                    "candidate": {field: candidate[field] for field in fields},
                }
            )
    return changed


def _execute_candidate(
    candidate_hash: str,
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    base_report: DevSourceScanReport,
    sources: tuple[Any, ...],
    all_parameters: tuple[DevParameterVersion, ...],
) -> dict[str, Any]:
    artifact = _load_stage_a(candidate_hash)
    accepted = _accepted(artifact)
    report = _candidate_report(base_report, accepted, parameter.content_hash, artifact)
    requirements = P1.build_market_data_requirement_plan(report)
    transport = P1.build_binance_transport_plan(
        report=report, requirements=requirements, max_attempts=3
    )
    # Keep the working path short enough for Windows MAX_PATH while retaining
    # the full candidate hash in every content-addressed artifact.
    candidate_dir = OUT_ROOT / candidate_hash[:12]
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
    if incremental is not None:
        _write_json(
            candidate_dir / f"{incremental.transport_plan_hash}.incremental-transport-plan.json",
            incremental.model_dump(mode="json"),
        )
        with httpx.Client(verify=False, timeout=60.0) as client:
            run_binance_transport(
                project_dir=ROOT,
                data_dir=ROOT / "artifacts" / "market_data",
                plan=incremental,
                approved_requirements_plan_hash=requirements.plan_hash,
                client=client,
            )
    _write_json(
        candidate_dir / "incremental-transport-requirements.json",
        {
            "base_transport_cache_reused": True,
            "candidate_transport_plan_hash": transport.transport_plan_hash,
            "incremental_transport_plan_hash": incremental.transport_plan_hash
            if incremental
            else None,
            "network_accessed": incremental is not None,
            "tasks": incremental_tasks,
        },
    )
    inc_candles, inc_funding, inc_observed = P1._load_incremental_rows(incremental)
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
    prep_dir.mkdir(parents=True, exist_ok=True)
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

    summary_module = P1._load_summary_module()
    _trade_row_patch(summary_module)
    scenario_summaries: dict[str, dict[str, Any]] = {}
    case_rows: dict[str, dict[str, dict[str, Any]]] = {}
    for scenario in CostScenario:
        result_dir = candidate_dir / f"results_{scenario.value}"
        checkpoint_path = result_dir / "checkpoint.json"
        checkpoint = run_replay_batch(
            project_dir=ROOT,
            manifest=prep.manifest,
            plan=plan,
            parameter_versions=all_parameters,
            market_data=market_data,
            funding_mark_supplements=supplements,
            results_dir=result_dir,
            checkpoint_path=checkpoint_path,
            candidate_hashes=(candidate_hash,),
            cost_scenarios=(scenario,),
        )
        if checkpoint.status != "COMPLETE":
            raise RuntimeError(f"{candidate_hash} {scenario.value} replay did not complete")
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
        case_rows[scenario.value] = _case_rows(result_dir)

    baseline_ids = {
        item.request.request.armed.logical_signal_id
        for item in P1.DevSourceScanReport.model_validate_json(
            (
                ROOT / "data" / "manifests" / "dev_source_scan" / f"{BASE_REPORT_HASH}.json"
            ).read_bytes()
        ).accepted_requests
    }
    accepted_ids = {item.request.request.armed.logical_signal_id for item in accepted}
    baseline_rows = {
        scenario.value: _case_rows(BASELINE_REPLAY_DIRS[scenario]) for scenario in CostScenario
    }
    baseline_summaries = {scenario.value: _baseline_summary(scenario) for scenario in CostScenario}
    zero_net = Decimal(str(scenario_summaries[CostScenario.ZERO.value]["portfolio"]["net_r"]))
    baseline_zero_net = Decimal(
        str(baseline_summaries[CostScenario.ZERO.value]["portfolio"]["net_r"])
    )
    for scenario in CostScenario:
        key = scenario.value
        scenario_summaries[key]["stage_b_metrics"] = _scenario_metrics(
            scenario_summaries[key],
            case_rows[key],
            baseline_summaries[key],
            baseline_rows[key],
            zero_net,
            baseline_zero_net,
        )
    special = {
        scenario.value: _special_breakdown(
            candidate_hash,
            scenario,
            case_rows[scenario.value],
            baseline_rows[scenario.value],
            accepted_ids,
            baseline_ids,
        )
        for scenario in CostScenario
    }
    ttl_outcome_changes = None
    if candidate_hash in {
        "0b515c4c262e0c2f23551a6db12e7fe56b7ca992e4c939e57deb6c690a474950",
        "3ba25f08b6ba9c05b96a2f6d54ca8b73aeda685b823f34aafd4e912f526e7631",
    }:
        ttl_outcome_changes = {
            scenario.value: _outcome_changes(
                case_rows[scenario.value],
                baseline_rows[scenario.value],
                accepted_ids & baseline_ids,
            )
            for scenario in CostScenario
        }
    data = {
        "candidate_hash": candidate_hash,
        "parameter_content_hash": parameter.content_hash,
        "stage_a_artifact_hash": artifact.artifact_hash,
        "source_final_ledger_hash": FINAL_LEDGER_HASH,
        "accepted_requests": len(accepted),
        "requirements_hash": requirements.plan_hash,
        "requirements_windows": sum(len(item.windows) for item in requirements.requirements),
        "requirements_one_minute_rows": requirements.one_minute_rows_after_overlap_dedup,
        "transport_plan_hash": transport.transport_plan_hash,
        "incremental_transport_plan_hash": incremental.transport_plan_hash if incremental else None,
        "incremental_tasks": incremental_tasks,
        "network_accessed": incremental is not None,
        "reused_candle_rows": max(
            0,
            requirements.one_minute_rows_after_overlap_dedup
            - sum(len(values) for values in inc_candles.values()),
        ),
        "reused_funding_rows": max(
            0,
            sum(len(item.settlement_times) for item in schedules)
            - sum(len(values) for values in inc_funding.values()),
        ),
        "new_candle_rows": sum(len(values) for values in inc_candles.values()),
        "new_funding_rows": sum(len(values) for values in inc_funding.values()),
        "new_guard_rows": sum(
            1
            for item in incremental_tasks
            if item["role"] in {"FUNDING_GUARD_BEFORE", "FUNDING_GUARD_AFTER"}
        ),
        "replay_ready": prep.summary.replay_ready_count,
        "blocked": prep.summary.not_ready_request_count,
        "supplement_hashes": [item.artifact_hash for item in supplements],
        "supplement_count": len(supplements),
        "warnings": sorted({item.warning for item in supplements}),
        "readiness_issues": [item.model_dump(mode="json") for item in prep.summary.issues],
        "scenario_summaries": scenario_summaries,
        "special_breakdown": special,
        "ttl_outcome_changes": ttl_outcome_changes,
        "prep_paths": [str(item) for item in prep_paths],
    }
    _write_json(candidate_dir / "stage_b_summary_v3.json", data)
    return data


def main() -> int:
    plan = SensitivityPlan.model_validate_json(
        (ROOT / "data" / "manifests" / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    base_report = DevSourceScanReport.model_validate_json(
        (ROOT / "data" / "manifests" / "dev_source_scan" / f"{BASE_REPORT_HASH}.json").read_bytes()
    )
    versions = {}
    for path in (ROOT / "data" / "manifests" / "parameter_version").glob("*.json"):
        version = DevParameterVersion.model_validate_json(path.read_bytes())
        if version.sensitivity_plan_hash == PLAN_HASH:
            versions[version.candidate.candidate_hash] = version
    candidates = tuple(SUPPORTED_CANDIDATES)[1:]
    sources = _merge_sources()
    results = [
        _execute_candidate(
            item, plan, versions[item], base_report, sources, tuple(versions.values())
        )
        for item in candidates
    ]
    output = {
        "phase": "P9 Phase 2 Stage B",
        "source_final_ledger_hash": FINAL_LEDGER_HASH,
        "candidates": results,
    }
    output = {**output, "summary_hash": P1._hash(output)}
    _write_json(OUT_ROOT / "phase2-stage-b-summary-v3.json", output)
    _write_json(OUT_ROOT / f"{output['summary_hash']}.phase2-stage-b-summary.json", output)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
