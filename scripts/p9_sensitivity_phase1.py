"""Execute the two frozen replay-only holding sensitivities from baseline P6 requests.

This is deliberately a narrow Phase-1 utility: it derives candidate-bound request
envelopes, reuses verified baseline raw rows where possible, downloads only missing
incremental ranges for max_holding_bars=48, prepares replay inputs, and runs the
three frozen cost scenarios.  It does not rescan P3-P6 or touch VALIDATION/
LOCKED_TEST.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from slagalpha.backtest.costs import CostScenario
from slagalpha.data.binance_usdm_transport import (
    BinanceTransportPlan,
    BinanceTransportTask,
    _task,
    build_binance_transport_plan,
    run_binance_transport,
)
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.batch_replay import run_replay_batch
from slagalpha.research.dev_source_scan import (
    AcceptedP6Request,
    DevMarketDataRequirement,
    DevSourceScanReport,
)
from slagalpha.research.funding_mark_supplement import (
    FundingMarkSupplementArtifact,
    FundingMarkSupplementRun,
    FundingMarkUncertaintyReport,
    fetch_funding_mark_event,
    quantify_funding_mark_uncertainty,
)
from slagalpha.research.market_data_requirements import (
    MarketDataRequirementPlan,
    _merged_windows,
    build_market_data_requirement_plan,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_inputs import DevReplayDataRequest
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    ReplayRestResponse,
    build_replay_market_data_artifact,
)
from slagalpha.research.replay_prep import (
    FundingScheduleArtifact,
    ReplayPrepResult,
    prepare_dev_replay,
    write_replay_prep_artifacts,
)
from slagalpha.research.sensitivity import SensitivityPlan


ROOT = Path(__file__).resolve().parents[1]
BASE_REPORT_HASH = "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2"
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
BASE_TRANSPORT_HASH = "d7915197e19d47aa86904d9d9cd184fc59e5df580e0e8282e8f89fd13259c769"
PARAMETERS_DIR = ROOT / "data" / "manifests" / "parameter_version"
BASE_ARTIFACT_DIR = ROOT / "artifacts" / "market_data" / "manifests" / "replay_market_data"
BASE_SCHEDULE_DIR = ROOT / "artifacts" / "market_data" / "manifests" / "funding_schedule"
BASE_SUPPLEMENT_DIR = ROOT / "artifacts" / "market_data" / "manifests" / "funding_mark_supplement"
BASE_TRANSPORT_RECEIPTS = (
    ROOT
    / "artifacts"
    / "market_data"
    / "transport_runs"
    / BASE_TRANSPORT_HASH
    / "receipts"
)
BASE_TRANSPORT_PLAN_PATH = (
    ROOT / "artifacts" / "market_data" / "transport_plans" / f"{BASE_TRANSPORT_HASH}.json"
)
OUT_ROOT = ROOT / "artifacts" / "sens_p1"


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    content = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise RuntimeError(f"immutable artifact changed: {path}")
        return
    path.write_bytes(content)


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise RuntimeError(f"immutable raw artifact changed: {path}")
        return
    path.write_bytes(content)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _interval_union(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for left, right in sorted(values):
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _subtract_interval(left: int, right: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cursor = left
    for cover_left, cover_right in _interval_union(covered):
        if cover_right <= cursor:
            continue
        if cover_left >= right:
            break
        if cover_left > cursor:
            result.append((cursor, min(cover_left, right)))
        cursor = max(cursor, cover_right)
        if cursor >= right:
            break
    if cursor < right:
        result.append((cursor, right))
    return [(a, b) for a, b in result if b > a]


def _request_end(request: DevReplayDataRequest, holding: int) -> datetime:
    expires = request.request.armed.expires_at
    last_entry = expires - timedelta(minutes=1)
    bucket = last_entry.replace(minute=(last_entry.minute // 15) * 15)
    return bucket + timedelta(minutes=15 * holding + 1)


def _candidate_requests(
    base: DevSourceScanReport, parameter_hash: str, holding: int
) -> tuple[AcceptedP6Request, ...]:
    accepted: list[AcceptedP6Request] = []
    for item in base.accepted_requests:
        old = item.request
        replay_request = old.request.model_copy(update={"max_holding_bars": holding})
        start = replay_request.armed.confirmation_close
        end = _request_end(old, holding)
        payload = old.model_dump(mode="json", exclude={"request_hash"})
        payload.update(
            {
                "parameter_content_hash": parameter_hash,
                "request": replay_request.model_dump(mode="json"),
                "start": _iso(start),
                "end_exclusive": _iso(end),
                "expected_candle_count": int((end - start).total_seconds() // 60),
            }
        )
        request = DevReplayDataRequest.model_validate(
            {**payload, "request_hash": _hash(payload)}
        )
        accepted.append(item.model_copy(update={"request": request}))
    return tuple(sorted(accepted, key=lambda item: (item.request.start, item.request.request.armed.symbol)))


def _candidate_report(
    base: DevSourceScanReport, accepted: tuple[AcceptedP6Request, ...], parameter_hash: str
) -> DevSourceScanReport:
    request_payload = [item.model_dump(mode="json") for item in accepted]
    request_set_hash = _hash({"accepted_requests": request_payload})
    grouped: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    for item in accepted:
        request = item.request
        grouped[request.request.armed.symbol].append(
            (request.start, request.end_exclusive, request.request_hash)
        )
    requirements = []
    for symbol in sorted(grouped):
        windows = _merged_windows(grouped[symbol])
        requirements.append(
            {
                "symbol": symbol,
                "windows": [item.model_dump(mode="json") for item in windows],
                "one_minute_record_count": sum(item.one_minute_record_count for item in windows),
                "funding_record_count_estimate": None,
            }
        )
    market_requirement = DevMarketDataRequirement(
        symbols=tuple(item["symbol"] for item in requirements),
        one_minute_rows_before_overlap_dedup=sum(
            item.request.expected_candle_count for item in accepted
        ),
        one_minute_rows_after_overlap_dedup=sum(
            item["one_minute_record_count"] for item in requirements
        ),
        funding_windows_before_overlap_dedup=len(accepted),
        funding_windows_after_overlap_dedup=sum(len(item["windows"]) for item in requirements),
        merged_window_minutes=sum(item["one_minute_record_count"] for item in requirements),
    )
    payload = base.model_dump(
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
            "accepted_requests": request_payload,
            "request_set_hash": request_set_hash,
            "market_data_requirement": market_requirement.model_dump(mode="json"),
        }
    )
    return DevSourceScanReport.model_validate({**payload, "report_hash": _hash(payload)})


def _load_baseline_sources() -> tuple[
    dict[str, dict[int, list[Any]]],
    dict[str, dict[int, dict[str, Any]]],
    dict[str, list[tuple[int, int]]],
    dict[str, list[tuple[int, int]]],
    dict[str, list[dict[str, Any]]],
    dict[str, datetime],
    dict[str, FundingMarkSupplementArtifact],
]:
    candles: dict[str, dict[int, list[Any]]] = defaultdict(dict)
    funding: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    candle_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    funding_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    observed: dict[str, datetime] = {}
    for path in BASE_ARTIFACT_DIR.glob("*.json"):
        artifact = ReplayMarketDataArtifact.model_validate_json(path.read_bytes())
        for response in artifact.responses:
            body = json.loads((ROOT / response.relative_path).read_text(encoding="utf-8"))
            observed[response.symbol] = max(observed.get(response.symbol, response.observed_at), response.observed_at)
            if artifact.role == "CANDLE_ONE_MINUTE":
                candle_intervals[response.symbol].append((response.start_time_ms, response.end_time_ms + 1))
                for row in body:
                    candles[response.symbol][int(row[0])] = row
            else:
                funding_intervals[response.symbol].append((response.start_time_ms, response.end_time_ms + 1))
                for row in body:
                    funding[response.symbol][int(row["fundingTime"])] = row
    guard_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    base_plan = BinanceTransportPlan.model_validate_json(BASE_TRANSPORT_PLAN_PATH.read_bytes())
    for task in base_plan.tasks:
        if task.role not in {"FUNDING_GUARD_BEFORE", "FUNDING_GUARD_AFTER"}:
            continue
        receipt = _load_json(BASE_TRANSPORT_RECEIPTS / f"{task.task_hash}.json")
        body_path = ROOT / receipt["blob_relative_path"]
        body = json.loads(body_path.read_text(encoding="utf-8"))
        observed[task.symbol] = max(
            observed.get(task.symbol, datetime(1970, 1, 1, tzinfo=UTC)),
            datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00")),
        )
        guard_rows[task.symbol].extend(body)
        for row in body:
            funding[task.symbol][int(row["fundingTime"])] = row
    for symbol in list(candle_intervals):
        candle_intervals[symbol] = _interval_union(candle_intervals[symbol])
    for symbol in list(funding_intervals):
        funding_intervals[symbol] = _interval_union(funding_intervals[symbol])
    supplements: dict[str, FundingMarkSupplementArtifact] = {}
    for path in BASE_SUPPLEMENT_DIR.glob("*.json"):
        item = FundingMarkSupplementArtifact.model_validate_json(path.read_bytes())
        supplements[item.request_hash] = item
    return candles, funding, candle_intervals, funding_intervals, guard_rows, observed, supplements


def _incremental_plan(
    report: DevSourceScanReport,
    requirements: MarketDataRequirementPlan,
    candidate_transport: BinanceTransportPlan,
    baseline_candle_intervals: dict[str, list[tuple[int, int]]],
    baseline_funding_intervals: dict[str, list[tuple[int, int]]],
    baseline_funding: dict[str, dict[int, dict[str, Any]]],
    baseline_guard_rows: dict[str, list[dict[str, Any]]],
    holding: int,
) -> tuple[BinanceTransportPlan | None, list[dict[str, Any]]]:
    tasks: list[BinanceTransportTask] = []
    descriptions: list[dict[str, Any]] = []
    for requirement in requirements.requirements:
        symbol = requirement.symbol
        covered_candle = baseline_candle_intervals.get(symbol, [])
        covered_funding = baseline_funding_intervals.get(symbol, [])
        for window in requirement.windows:
            left = int(window.start.timestamp() * 1000)
            right = int(window.end_exclusive.timestamp() * 1000)
            for role, covered in (
                ("CANDLE_ONE_MINUTE", covered_candle),
                ("FUNDING", covered_funding),
            ):
                for missing_left, missing_right in _subtract_interval(left, right, covered):
                    task = _task(
                        requirements_plan_hash=requirements.plan_hash,
                        role=role,
                        symbol=symbol,
                        start=datetime.fromtimestamp(missing_left / 1000, UTC),
                        end_exclusive=datetime.fromtimestamp(missing_right / 1000, UTC),
                        request_hashes=window.request_hashes,
                        max_attempts=3,
                    )
                    tasks.append(task)
                    descriptions.append(
                        {
                            "role": role,
                            "symbol": symbol,
                            "start": _iso(datetime.fromtimestamp(missing_left / 1000, UTC)),
                            "end_exclusive": _iso(datetime.fromtimestamp(missing_right / 1000, UTC)),
                            "reason": "candidate horizon beyond verified baseline transport coverage",
                            "task_hash": task.task_hash,
                        }
                    )
            if holding != 48:
                continue
            existing_after = [
                int(row["fundingTime"])
                for row in baseline_guard_rows.get(symbol, [])
                if int(row["fundingTime"]) >= right
            ]
            if existing_after:
                continue
            task = _task(
                requirements_plan_hash=requirements.plan_hash,
                role="FUNDING_GUARD_AFTER",
                symbol=symbol,
                start=window.end_exclusive,
                end_exclusive=window.end_exclusive + timedelta(days=30),
                request_hashes=window.request_hashes,
                max_attempts=3,
            )
            tasks.append(task)
            descriptions.append(
                {
                    "role": "FUNDING_GUARD_AFTER",
                    "symbol": symbol,
                    "start": _iso(window.end_exclusive),
                    "end_exclusive": _iso(window.end_exclusive + timedelta(days=30)),
                    "reason": "candidate-specific post-window settlement guard",
                    "task_hash": task.task_hash,
                }
            )
    # Deduplicate identical query ranges while retaining the complete accepted hash set.
    by_key: dict[tuple[str, str, int, int], BinanceTransportTask] = {}
    for task in tasks:
        key = (task.role, task.symbol, task.start_time_ms, task.end_time_ms)
        prior = by_key.get(key)
        if prior is None or len(task.accepted_request_hashes) > len(prior.accepted_request_hashes):
            by_key[key] = task
    tasks = sorted(by_key.values(), key=lambda item: item.task_hash)
    descriptions = [
        item
        for item in descriptions
        if any(item["task_hash"] == task.task_hash for task in tasks)
    ]
    if not tasks:
        return None, descriptions
    payload = {
        "schema_version": "binance-usdm-transport-plan/0.2.0",
        "requirements_plan_hash": requirements.plan_hash,
        "source_report_hash": report.report_hash,
        "request_set_hash": report.request_set_hash,
        "accepted_request_hashes": tuple(sorted(item.request.request_hash for item in report.accepted_requests)),
        "tasks": [item.model_dump(mode="json") for item in tasks],
        "network_authorized": False,
    }
    plan = BinanceTransportPlan.model_validate({**payload, "transport_plan_hash": _hash(payload)})
    if plan.transport_plan_hash == candidate_transport.transport_plan_hash:
        raise RuntimeError("incremental plan unexpectedly equals full candidate transport plan")
    return plan, descriptions


def _load_incremental_rows(
    plan: BinanceTransportPlan | None,
) -> tuple[dict[str, dict[int, list[Any]]], dict[str, dict[int, dict[str, Any]]], dict[str, datetime]]:
    candles: dict[str, dict[int, list[Any]]] = defaultdict(dict)
    funding: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    observed: dict[str, datetime] = {}
    if plan is None:
        return candles, funding, observed
    receipts = ROOT / "artifacts" / "market_data" / "transport_runs" / plan.transport_plan_hash / "receipts"
    for task in plan.tasks:
        receipt = _load_json(receipts / f"{task.task_hash}.json")
        body = json.loads((ROOT / receipt["blob_relative_path"]).read_text(encoding="utf-8"))
        when = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
        observed[task.symbol] = max(observed.get(task.symbol, when), when)
        if task.role == "CANDLE_ONE_MINUTE":
            for row in body:
                candles[task.symbol][int(row[0])] = row
        else:
            for row in body:
                funding[task.symbol][int(row["fundingTime"])] = row
    return candles, funding, observed


def _candidate_market_data(
    *,
    candidate_dir: Path,
    report: DevSourceScanReport,
    requirements: MarketDataRequirementPlan,
    transport: BinanceTransportPlan,
    holding: int,
    base_candles: dict[str, dict[int, list[Any]]],
    base_funding: dict[str, dict[int, dict[str, Any]]],
    base_observed: dict[str, datetime],
    incremental_candles: dict[str, dict[int, list[Any]]],
    incremental_funding: dict[str, dict[int, dict[str, Any]]],
    incremental_observed: dict[str, datetime],
) -> tuple[tuple[ReplayMarketDataArtifact, ...], tuple[FundingScheduleArtifact, ...]]:
    candle_rows: dict[str, dict[int, list[Any]]] = defaultdict(dict)
    funding_rows: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for source, target in ((base_candles, candle_rows), (incremental_candles, candle_rows)):
        for symbol, values in source.items():
            target[symbol].update(values)
    for source, target in ((base_funding, funding_rows), (incremental_funding, funding_rows)):
        for symbol, values in source.items():
            target[symbol].update(values)
    observed = dict(base_observed)
    for symbol, when in incremental_observed.items():
        observed[symbol] = max(observed.get(symbol, when), when)
    raw_root = candidate_dir / "raw"
    artifact_root = candidate_dir / "market_data"
    artifact_root.mkdir(parents=True, exist_ok=True)
    schedules: list[FundingScheduleArtifact] = []
    schedule_by_window: dict[tuple[str, datetime, datetime], FundingScheduleArtifact] = {}
    for requirement in requirements.requirements:
        symbol = requirement.symbol
        for window in requirement.windows:
            left = int(window.start.timestamp() * 1000)
            right = int(window.end_exclusive.timestamp() * 1000)
            rows = [
                row
                for timestamp, row in sorted(funding_rows.get(symbol, {}).items())
                if left <= timestamp < right
            ]
            times = tuple(
                datetime.fromtimestamp(int(row["fundingTime"]) / 1000, UTC) for row in rows
            )
            evidence_hash = _hash(
                {
                    "transport_plan_hash": transport.transport_plan_hash,
                    "symbol": symbol,
                    "start": _iso(window.start),
                    "end_exclusive": _iso(window.end_exclusive),
                    "funding_rows": rows,
                }
            )
            schedule_payload = {
                "symbol": symbol,
                "coverage_start": window.start,
                "coverage_end_exclusive": window.end_exclusive,
                "settlement_times": times,
                "provider": "BINANCE_USDM",
                "schedule_version": "candidate-derived-from-verified-transport/1",
                "source_ref": (
                    f"transport:{transport.transport_plan_hash}:{symbol}:"
                    f"{left}:{right - 1}"
                ),
                "source_content_sha256": evidence_hash,
                "observed_at": observed[symbol],
            }
            from slagalpha.research.replay_prep import build_funding_schedule_artifact

            schedule = build_funding_schedule_artifact(**schedule_payload)
            _write_json(
                candidate_dir / "funding_schedule" / f"{schedule.schedule_hash}.json",
                schedule.model_dump(mode="json"),
            )
            schedules.append(schedule)
            schedule_by_window[(symbol, window.start, window.end_exclusive)] = schedule

    artifacts: list[ReplayMarketDataArtifact] = []
    for accepted in report.accepted_requests:
        request = accepted.request
        symbol = request.request.armed.symbol
        left = int(request.start.timestamp() * 1000)
        right = int(request.end_exclusive.timestamp() * 1000)
        candle = [
            candle_rows.get(symbol, {}).get(timestamp)
            for timestamp in range(left, right, 60_000)
        ]
        if any(row is None for row in candle):
            missing = next(timestamp for timestamp, row in zip(range(left, right, 60_000), candle) if row is None)
            raise RuntimeError(f"Candle coverage missing for {symbol} at {missing}")
        candle = [row for row in candle if row is not None]
        candle_bytes = json.dumps(candle, ensure_ascii=False, separators=(",", ":")).encode()
        candle_body_hash = hashlib.sha256(candle_bytes).hexdigest()
        candle_path = raw_root / "candle" / f"{candle_body_hash}.json"
        _write_bytes(candle_path, candle_bytes)
        candle_response = ReplayRestResponse(
            endpoint="/fapi/v1/klines",
            symbol=symbol,
            interval="1m",
            start_time_ms=left,
            end_time_ms=right - 1,
            limit=len(candle),
            observed_at=observed[symbol],
            relative_path=candle_path.relative_to(ROOT).as_posix(),
            body_sha256=candle_body_hash,
        )
        candle_artifact, _ = build_replay_market_data_artifact(
            project_dir=ROOT,
            request=request,
            role="CANDLE_ONE_MINUTE",
            responses=(candle_response,),
        )
        _write_json(
            artifact_root / f"{candle_artifact.artifact_hash}.json",
            candle_artifact.model_dump(mode="json"),
        )
        artifacts.append(candle_artifact)

        matching = [
            window
            for item in requirements.requirements
            if item.symbol == symbol
            for window in item.windows
            if window.start <= request.start and window.end_exclusive >= request.end_exclusive
        ]
        if len(matching) != 1:
            raise RuntimeError(f"cannot bind candidate schedule for {request.request_hash}")
        schedule = schedule_by_window[(symbol, matching[0].start, matching[0].end_exclusive)]
        required_times = tuple(
            value
            for value in schedule.settlement_times
            if request.start < value < request.end_exclusive - timedelta(minutes=1)
        )
        if not required_times:
            continue
        funding = [
            funding_rows[symbol][int(value.timestamp() * 1000)]
            for value in required_times
            if int(value.timestamp() * 1000) in funding_rows.get(symbol, {})
        ]
        if len(funding) != len(required_times):
            raise RuntimeError(f"Funding coverage missing for {symbol} request {request.request_hash}")
        funding_bytes = json.dumps(funding, ensure_ascii=False, separators=(",", ":")).encode()
        funding_body_hash = hashlib.sha256(funding_bytes).hexdigest()
        funding_path = raw_root / "funding" / f"{funding_body_hash}.json"
        _write_bytes(funding_path, funding_bytes)
        funding_response = ReplayRestResponse(
            endpoint="/fapi/v1/fundingRate",
            symbol=symbol,
            interval=None,
            start_time_ms=left,
            end_time_ms=right - 1,
            limit=1000,
            observed_at=observed[symbol],
            relative_path=funding_path.relative_to(ROOT).as_posix(),
            body_sha256=funding_body_hash,
        )
        funding_artifact, _ = build_replay_market_data_artifact(
            project_dir=ROOT,
            request=request,
            role="FUNDING",
            responses=(funding_response,),
        )
        _write_json(
            artifact_root / f"{funding_artifact.artifact_hash}.json",
            funding_artifact.model_dump(mode="json"),
        )
        artifacts.append(funding_artifact)
    return tuple(sorted(artifacts, key=lambda item: item.artifact_hash)), tuple(
        sorted(schedules, key=lambda item: item.schedule_hash)
    )


def _candidate_supplements(
    *,
    candidate_dir: Path,
    report: DevSourceScanReport,
    artifacts: tuple[ReplayMarketDataArtifact, ...],
    baseline_supplements: dict[str, FundingMarkSupplementArtifact],
) -> tuple[FundingMarkSupplementArtifact, ...]:
    funding_by_request = {
        item.request_hash: item
        for item in artifacts
        if item.role == "FUNDING"
    }
    supplements: list[FundingMarkSupplementArtifact] = []
    for accepted in report.accepted_requests:
        request = accepted.request
        funding = funding_by_request.get(request.request_hash)
        if funding is None:
            continue
        rows = []
        for response in funding.responses:
            rows.extend(json.loads((ROOT / response.relative_path).read_text(encoding="utf-8")))
        for row in rows:
            if row.get("markPrice") not in {None, ""}:
                continue
            baseline = next(
                (
                    item
                    for item in baseline_supplements.values()
                    if item.symbol == request.request.armed.symbol
                    and item.funding_time == datetime.fromtimestamp(int(row["fundingTime"]) / 1000, UTC)
                ),
                None,
            )
            if baseline is None:
                funding_time = datetime.fromtimestamp(int(row["fundingTime"]) / 1000, UTC)
                existing = None
                candidate_supplement_dir = candidate_dir / "funding_mark_supplement"
                if candidate_supplement_dir.exists():
                    for path in candidate_supplement_dir.glob("*.json"):
                        item = FundingMarkSupplementArtifact.model_validate_json(path.read_bytes())
                        if (
                            item.request_hash == request.request_hash
                            and item.funding_artifact_hash == funding.artifact_hash
                            and item.symbol == request.request.armed.symbol
                            and item.funding_time == funding_time
                        ):
                            existing = item
                            break
                if existing is not None:
                    supplement = existing
                else:
                    try:
                        funding_rate = Decimal(str(row["fundingRate"]))
                    except (KeyError, InvalidOperation, ValueError) as error:
                        raise RuntimeError(
                            f"candidate Funding row has invalid fundingRate: "
                            f"{request.request.armed.symbol} {row.get('fundingTime')}"
                        ) from error
                    # This is a genuinely new max48 settlement event.  Recheck the
                    # exact Funding row and its exact 1m mark-price candle, then
                    # bind both responses to this candidate request/artifact.
                    with httpx.Client(verify=False, timeout=60.0) as client:
                        supplement = fetch_funding_mark_event(
                            client=client,
                            project_dir=ROOT,
                            data_dir=ROOT / "artifacts" / "market_data",
                            request_hash=request.request_hash,
                            funding_artifact_hash=funding.artifact_hash,
                            symbol=request.request.armed.symbol,
                            funding_time=funding_time,
                            funding_rate=funding_rate,
                        )
            else:
                payload = baseline.model_dump(
                    mode="json",
                    exclude={"artifact_hash", "request_hash", "funding_artifact_hash"},
                )
                payload.update(
                    {
                        "request_hash": request.request_hash,
                        "funding_artifact_hash": funding.artifact_hash,
                    }
                )
                supplement = FundingMarkSupplementArtifact.model_validate(
                    {**payload, "artifact_hash": _hash(payload)}
                )
            _write_json(
                candidate_dir / "funding_mark_supplement" / f"{supplement.artifact_hash}.json",
                supplement.model_dump(mode="json"),
            )
            supplements.append(supplement)
    unique = {item.artifact_hash: item for item in supplements}
    return tuple(sorted(unique.values(), key=lambda item: item.artifact_hash))


def _write_candidate_supplement_indexes(
    *,
    candidate_dir: Path,
    report: DevSourceScanReport,
    requirements: MarketDataRequirementPlan,
    transport: BinanceTransportPlan,
    supplements: tuple[FundingMarkSupplementArtifact, ...],
) -> tuple[Path, Path]:
    by_request = {item.request.request_hash: item.request for item in report.accepted_requests}
    items = tuple(
        sorted(
            (
                quantify_funding_mark_uncertainty(
                    request=by_request[item.request_hash], artifact=item
                )
                for item in supplements
            ),
            key=lambda item: (item.request_hash, item.funding_time),
        )
    )
    uncertainty_payload = {
        "schema_version": "funding-mark-uncertainty/0.1.0",
        "supplement_artifact_hashes": [item.artifact_hash for item in supplements],
        "items": [item.model_dump(mode="json") for item in items],
    }
    uncertainty = FundingMarkUncertaintyReport.model_validate(
        {**uncertainty_payload, "report_hash": _hash(uncertainty_payload)}
    )
    uncertainty_path = candidate_dir / "funding_mark_uncertainty" / f"{uncertainty.report_hash}.json"
    _write_json(uncertainty_path, uncertainty.model_dump(mode="json"))
    raw_body_bytes = 0
    for item in supplements:
        raw_body_bytes += (ROOT / item.funding_history_relative_path).stat().st_size
        if item.mark_price_relative_path:
            raw_body_bytes += (ROOT / item.mark_price_relative_path).stat().st_size
    run_payload = {
        "schema_version": "funding-mark-supplement-run/0.1.0",
        "requirements_plan_hash": requirements.plan_hash,
        "transport_plan_hash": transport.transport_plan_hash,
        "source_report_hash": report.report_hash,
        "artifacts": sorted(item.artifact_hash for item in supplements),
        "raw_body_bytes": raw_body_bytes,
    }
    run = FundingMarkSupplementRun.model_validate({**run_payload, "artifact_hash": _hash(run_payload)})
    run_path = candidate_dir / "funding_mark_supplement_run" / f"{run.artifact_hash}.json"
    _write_json(run_path, run.model_dump(mode="json"))
    return run_path, uncertainty_path


def _copy_unique_parameters(candidate_dir: Path, versions: tuple[DevParameterVersion, ...]) -> Path:
    target = candidate_dir / "parameters"
    target.mkdir(parents=True, exist_ok=True)
    for version in versions:
        source = PARAMETERS_DIR / f"{version.content_hash}.json"
        _write_bytes(target / source.name, source.read_bytes())
    return target


def _load_summary_module():
    path = ROOT / "scripts" / "p9_replay_summary.py"
    spec = importlib.util.spec_from_file_location("p9_replay_summary_phase1", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load replay summary builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execute_candidate(
    *,
    candidate_hash: str,
    parameter: DevParameterVersion,
    plan: SensitivityPlan,
    base_report: DevSourceScanReport,
    base_candles: dict[str, dict[int, list[Any]]],
    base_funding: dict[str, dict[int, dict[str, Any]]],
    base_candle_intervals: dict[str, list[tuple[int, int]]],
    base_funding_intervals: dict[str, list[tuple[int, int]]],
    base_guard_rows: dict[str, list[dict[str, Any]]],
    base_observed: dict[str, datetime],
    base_supplements: dict[str, FundingMarkSupplementArtifact],
    all_parameters: tuple[DevParameterVersion, ...],
) -> dict[str, Any]:
    holding = parameter.candidate.parameters.max_holding_bars
    candidate_dir = OUT_ROOT / f"h{holding}_{candidate_hash[:12]}"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    accepted = _candidate_requests(base_report, parameter.content_hash, holding)
    report = _candidate_report(base_report, accepted, parameter.content_hash)
    requirements = build_market_data_requirement_plan(report)
    transport = build_binance_transport_plan(report=report, requirements=requirements, max_attempts=3)
    _write_json(candidate_dir / f"{report.report_hash}.candidate-source-scan.json", report.model_dump(mode="json"))
    _write_json(candidate_dir / f"{requirements.plan_hash}.requirements.json", requirements.model_dump(mode="json"))
    _write_json(candidate_dir / f"{transport.transport_plan_hash}.transport-plan.json", transport.model_dump(mode="json"))
    _write_json(
        candidate_dir / "requests.json",
        {"candidate_hash": candidate_hash, "request_count": len(accepted), "requests": [item.model_dump(mode="json") for item in accepted]},
    )
    incremental, incremental_tasks = _incremental_plan(
        report,
        requirements,
        transport,
        base_candle_intervals,
        base_funding_intervals,
        base_funding,
        base_guard_rows,
        holding,
    )
    if incremental is not None:
        _write_json(candidate_dir / f"{incremental.transport_plan_hash}.incremental-transport-plan.json", incremental.model_dump(mode="json"))
        run_binance_transport(
            project_dir=ROOT,
            data_dir=ROOT / "artifacts" / "market_data",
            plan=incremental,
            approved_requirements_plan_hash=requirements.plan_hash,
            client=httpx.Client(verify=False, timeout=60.0),
        )
    _write_json(
        candidate_dir / "incremental-transport-requirements.json",
        {
            "candidate_hash": candidate_hash,
            "base_transport_plan_hash": BASE_TRANSPORT_HASH,
            "candidate_transport_plan_hash": transport.transport_plan_hash,
            "incremental_transport_plan_hash": incremental.transport_plan_hash if incremental else None,
            "network_accessed": incremental is not None,
            "tasks": incremental_tasks,
        },
    )
    inc_candles, inc_funding, inc_observed = _load_incremental_rows(incremental)
    artifacts, schedules = _candidate_market_data(
        candidate_dir=candidate_dir,
        report=report,
        requirements=requirements,
        transport=transport,
        holding=holding,
        base_candles=base_candles,
        base_funding=base_funding,
        base_observed=base_observed,
        incremental_candles=inc_candles,
        incremental_funding=inc_funding,
        incremental_observed=inc_observed,
    )
    supplements = _candidate_supplements(
        candidate_dir=candidate_dir,
        report=report,
        artifacts=artifacts,
        baseline_supplements=base_supplements,
    )
    prep: ReplayPrepResult = prepare_dev_replay(
        project_dir=ROOT,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
        funding_mark_supplements=supplements,
        requirements=requirements,
        transport_plan=transport,
    )
    (candidate_dir / "replay_prep").mkdir(parents=True, exist_ok=True)
    prep_paths = write_replay_prep_artifacts(prep, candidate_dir / "replay_prep")
    _write_json(candidate_dir / "replay_prep" / "summary.json", prep.summary.model_dump(mode="json"))
    _write_json(candidate_dir / "replay_prep" / "manifest.json", prep.manifest.model_dump(mode="json"))
    params_dir = _copy_unique_parameters(candidate_dir, all_parameters)
    run_path, uncertainty_path = _write_candidate_supplement_indexes(
        candidate_dir=candidate_dir,
        report=report,
        requirements=requirements,
        transport=transport,
        supplements=supplements,
    )
    summary_module = _load_summary_module()

    # The shared summary helper historically derives risk as gross_pnl/gross_r.
    # A valid flat TIME_EXIT can have both values equal to zero; use the frozen
    # Entry/Stop distance in that one case without altering the replay result.
    original_trade_row = summary_module._trade_row

    def safe_trade_row(result):
        if result.gross_r != 0:
            return original_trade_row(result)
        if result.p7_summary is None:
            raise ValueError("flat result is missing its P7 summary")
        canonical = json.loads(result.p7_summary.canonical_json)
        metrics = canonical["metrics"]
        risk = abs(Decimal(canonical["plan"]["entry"]) - Decimal(canonical["plan"]["stop"]))
        if risk == 0:
            raise ValueError("flat result has zero Entry/Stop risk")
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
    build_summary = summary_module.build_summary
    scenario_summaries: dict[str, dict[str, Any]] = {}
    for scenario in CostScenario:
        result_dir = candidate_dir / f"results_{scenario.value}"
        checkpoint_path = result_dir / "checkpoint.json"
        checkpoint = run_replay_batch(
            project_dir=ROOT,
            manifest=prep.manifest,
            plan=plan,
            parameter_versions=all_parameters,
            market_data=artifacts,
            funding_mark_supplements=supplements,
            results_dir=result_dir,
            checkpoint_path=checkpoint_path,
            candidate_hashes=(candidate_hash,),
            cost_scenarios=(scenario,),
        )
        if checkpoint.status != "COMPLETE":
            raise RuntimeError(f"{candidate_hash} {scenario.value} batch did not complete")
        summary = build_summary(
            results_dir=result_dir,
            checkpoint_path=checkpoint_path,
            readiness_hash=prep.summary.summary_hash,
            supplement_run_path=run_path,
            uncertainty_path=uncertainty_path,
            supplement_dir=candidate_dir / "funding_mark_supplement",
        )
        _write_json(result_dir / f"{summary['summary_hash']}.summary.json", summary)
        scenario_summaries[scenario.value] = summary
    return {
        "candidate_hash": candidate_hash,
        "holding": holding,
        "parameter_content_hash": parameter.content_hash,
        "source_report_hash": report.report_hash,
        "request_set_hash": report.request_set_hash,
        "request_count": len(accepted),
        "requirements_hash": requirements.plan_hash,
        "transport_plan_hash": transport.transport_plan_hash,
        "incremental_transport_plan_hash": incremental.transport_plan_hash if incremental else None,
        "incremental_tasks": incremental_tasks,
        "network_accessed": incremental is not None,
        "new_candle_windows": [
            item for item in incremental_tasks if item["role"] == "CANDLE_ONE_MINUTE"
        ],
        "new_funding_windows": [
            item for item in incremental_tasks if item["role"] in {"FUNDING", "FUNDING_GUARD_AFTER"}
        ],
        "ready_count": prep.summary.replay_ready_count,
        "readiness_hash": prep.summary.summary_hash,
        "manifest_hash": prep.manifest.manifest_hash,
        "supplement_hashes": [item.artifact_hash for item in supplements],
        "scenario_summaries": scenario_summaries,
        "output_dir": str(candidate_dir),
        "prep_paths": [str(item) for item in prep_paths],
    }


def main() -> int:
    base_report = DevSourceScanReport.model_validate_json(
        (ROOT / "data" / "manifests" / "dev_source_scan" / f"{BASE_REPORT_HASH}.json").read_bytes()
    )
    plan = SensitivityPlan.model_validate_json(
        (ROOT / "data" / "manifests" / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    versions_by_candidate: dict[str, DevParameterVersion] = {}
    by_content: dict[str, DevParameterVersion] = {}
    for path in PARAMETERS_DIR.glob("*.json"):
        version = DevParameterVersion.model_validate_json(path.read_bytes())
        if version.sensitivity_plan_hash != PLAN_HASH:
            continue
        by_content[version.content_hash] = version
        versions_by_candidate[version.candidate.candidate_hash] = version
    all_parameters = tuple(sorted(by_content.values(), key=lambda item: item.content_hash))
    if len(all_parameters) != 10:
        raise RuntimeError(f"expected ten unique frozen parameter versions, got {len(all_parameters)}")
    target_hashes = (
        "fbc968495d574e8a71fdccf600a4059df0cd42a401e843bc5f6cd83683f833f4",
        "65ffe100de22e6150828c607e97ae5272739d02a4cf9bd2477cb924e7f3fa6f3",
    )
    base_candles, base_funding, base_candle_intervals, base_funding_intervals, base_guard_rows, base_observed, base_supplements = _load_baseline_sources()
    results = []
    for candidate_hash in target_hashes:
        parameter = versions_by_candidate[candidate_hash]
        results.append(
            _execute_candidate(
                candidate_hash=candidate_hash,
                parameter=parameter,
                plan=plan,
                base_report=base_report,
                base_candles=base_candles,
                base_funding=base_funding,
                base_candle_intervals=base_candle_intervals,
                base_funding_intervals=base_funding_intervals,
                base_guard_rows=base_guard_rows,
                base_observed=base_observed,
                base_supplements=base_supplements,
                all_parameters=all_parameters,
            )
        )
    output = {"phase": "Sensitivity Execution Planning / Phase 1", "candidates": results}
    _write_json(OUT_ROOT / "phase1-summary.json", output)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
