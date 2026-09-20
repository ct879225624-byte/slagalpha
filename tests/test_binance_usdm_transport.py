"""Synthetic-only tests for accepted-request-scoped Binance USD-M transport."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from slagalpha.data.binance_usdm_transport import (
    BinanceTransportError,
    TransportReceipt,
    build_binance_transport_plan,
    finalize_binance_transport,
    run_binance_transport,
)
from slagalpha.research.market_data_requirements import (
    build_market_data_requirement_plan,
)
from slagalpha.research.replay_prep import prepare_dev_replay
from test_post_scan_support import _scan_report


def _kline(open_time: int) -> list[Any]:
    return [
        open_time,
        "100",
        "101",
        "99",
        "100",
        "2",
        open_time + 59_999,
        "200",
        2,
        "1",
        "100",
        "0",
    ]


def _body(request: httpx.Request, funding_times: tuple[int, ...]) -> list[Any]:
    values = dict(request.url.params)
    start = int(values["startTime"])
    end = int(values["endTime"])
    if request.url.path == "/fapi/v1/klines":
        return [_kline(value) for value in range(start, end + 1, 60_000)]
    return [
        {
            "symbol": values["symbol"],
            "fundingTime": funding_time,
            "fundingRate": "-0.0001",
            "markPrice": "100",
            "rateType": "Regular",
        }
        for funding_time in funding_times
        if start <= funding_time <= end
    ][: int(values["limit"])]


def _client(
    funding_times: tuple[int, ...],
    calls: Counter[str],
    *,
    first_status: int | None = None,
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        calls[key] += 1
        if first_status is not None and sum(calls.values()) == 1:
            return httpx.Response(first_status, json={"code": -1}, request=request)
        return httpx.Response(
            200,
            content=json.dumps(
                _body(request, funding_times), separators=(",", ":")
            ).encode(),
            headers={"content-type": "application/json", "etag": '"synthetic"'},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _inputs() -> tuple[Any, Any, Any]:
    report = _scan_report()
    requirements = build_market_data_requirement_plan(report)
    plan = build_binance_transport_plan(report=report, requirements=requirements)
    return report, requirements, plan


def test_plan_authenticates_requirements_and_deduplicates_overlap() -> None:
    report, requirements, plan = _inputs()
    candle_tasks = sorted(
        (item for item in plan.tasks if item.role == "CANDLE_ONE_MINUTE"),
        key=lambda item: item.start_time_ms,
    )
    assert len(plan.tasks) == 8
    assert sum(item.limit for item in candle_tasks) == 541
    assert all(
        current.start_time_ms == previous.end_time_ms + 1
        for previous, current in zip(candle_tasks, candle_tasks[1:], strict=False)
    )
    assert {value for item in plan.tasks for value in item.accepted_request_hashes} == {
        item.request.request_hash for item in report.accepted_requests
    }
    assert Counter(item.role for item in plan.tasks) == Counter(
        {
            "CANDLE_ONE_MINUTE": 3,
            "FUNDING": 3,
            "FUNDING_GUARD_BEFORE": 1,
            "FUNDING_GUARD_AFTER": 1,
        }
    )

    changed = requirements.model_copy(update={"source_report_hash": "0" * 64})
    with pytest.raises(ValueError, match="plan hash mismatch"):
        build_binance_transport_plan(report=report, requirements=changed)


def test_download_resume_cache_provenance_and_replay_handoff(tmp_path: Path) -> None:
    report, requirements, plan = _inputs()
    funding_time = int(
        (report.accepted_requests[0].request.start + timedelta(hours=2)).timestamp()
        * 1000
    )
    window = requirements.requirements[0].windows[0]
    funding_times = (
        int((window.start - timedelta(hours=3)).timestamp() * 1000),
        funding_time,
        int((window.end_exclusive + timedelta(hours=5)).timestamp() * 1000),
    )
    calls: Counter[str] = Counter()
    data_dir = tmp_path / "market"
    observed_at = report.accepted_requests[-1].request.end_exclusive + timedelta(days=1)
    with _client(funding_times, calls) as client:
        checkpoint = run_binance_transport(
            project_dir=tmp_path,
            data_dir=data_dir,
            plan=plan,
            approved_requirements_plan_hash=requirements.plan_hash,
            client=client,
            now=lambda: observed_at,
            sleep=lambda _: None,
        )
        first_call_count = sum(calls.values())
        resumed = run_binance_transport(
            project_dir=tmp_path,
            data_dir=data_dir,
            plan=plan,
            approved_requirements_plan_hash=requirements.plan_hash,
            client=client,
            now=lambda: observed_at,
            sleep=lambda _: None,
        )
    assert checkpoint == resumed
    assert first_call_count == len(plan.tasks) == sum(calls.values())
    assert all(item.status == "COMPLETE" for item in checkpoint.entries)

    receipts = tuple(
        TransportReceipt.model_validate_json(path.read_bytes())
        for path in sorted(
            (
                data_dir
                / "transport_runs"
                / plan.transport_plan_hash
                / "receipts"
            ).glob("*.json")
        )
    )
    assert len(receipts) == len(plan.tasks)
    assert all(
        item.request_url.startswith("https://fapi.binance.com/fapi/v1/")
        for item in receipts
    )
    assert all(
        hashlib.sha256((tmp_path / item.blob_relative_path).read_bytes()).hexdigest()
        == item.body_sha256
        for item in receipts
    )
    assert len({item.body_sha256 for item in receipts}) < len(receipts)

    outputs = finalize_binance_transport(
        project_dir=tmp_path,
        data_dir=data_dir,
        report=report,
        requirements=requirements,
        plan=plan,
    )
    assert len(outputs.market_data) == 4
    assert len(outputs.funding_schedules) == 1
    assert outputs.funding_schedules[0].settlement_times == (
        datetime.fromtimestamp(funding_time / 1000, UTC),
    )
    prepared = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=outputs.market_data,
        funding_schedules=outputs.funding_schedules,
    )
    assert prepared.summary.can_start_dev_replay is True


def test_zero_funding_window_requires_observed_events_on_both_sides(
    tmp_path: Path,
) -> None:
    report, requirements, plan = _inputs()
    window = requirements.requirements[0].windows[0]
    before = int((window.start - timedelta(hours=7)).timestamp() * 1000)
    after = int((window.end_exclusive + timedelta(hours=3)).timestamp() * 1000)
    observed_at = window.end_exclusive + timedelta(days=40)

    calls: Counter[str] = Counter()
    data_dir = tmp_path / "e"
    with _client((before, after), calls) as client:
        run_binance_transport(
            project_dir=tmp_path,
            data_dir=data_dir,
            plan=plan,
            approved_requirements_plan_hash=requirements.plan_hash,
            client=client,
            now=lambda: observed_at,
            sleep=lambda _: None,
        )
    outputs = finalize_binance_transport(
        project_dir=tmp_path,
        data_dir=data_dir,
        report=report,
        requirements=requirements,
        plan=plan,
    )
    assert outputs.funding_schedules[0].settlement_times == ()
    assert {item.role for item in outputs.market_data} == {"CANDLE_ONE_MINUTE"}
    assert prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=outputs.market_data,
        funding_schedules=outputs.funding_schedules,
    ).summary.can_start_dev_replay

    for name, incomplete_times in (("mb", (after,)), ("ma", (before,))):
        incomplete_dir = tmp_path / name
        with _client(incomplete_times, Counter()) as client:
            run_binance_transport(
                project_dir=tmp_path,
                data_dir=incomplete_dir,
                plan=plan,
                approved_requirements_plan_hash=requirements.plan_hash,
                client=client,
                now=lambda: observed_at,
                sleep=lambda _: None,
            )
        with pytest.raises(BinanceTransportError, match="adjacent observed settlement"):
            finalize_binance_transport(
                project_dir=tmp_path,
                data_dir=incomplete_dir,
                report=report,
                requirements=requirements,
                plan=plan,
            )


def test_boundary_settlements_are_evidence_but_not_interior_funding(
    tmp_path: Path,
) -> None:
    report, requirements, plan = _inputs()
    window = requirements.requirements[0].windows[0]
    times = (
        int((window.start - timedelta(hours=4)).timestamp() * 1000),
        int(window.start.timestamp() * 1000),
        int(window.end_exclusive.timestamp() * 1000),
    )
    data_dir = tmp_path / "b"
    with _client(times, Counter()) as client:
        run_binance_transport(
            project_dir=tmp_path,
            data_dir=data_dir,
            plan=plan,
            approved_requirements_plan_hash=requirements.plan_hash,
            client=client,
            now=lambda: window.end_exclusive + timedelta(days=40),
            sleep=lambda _: None,
        )
    outputs = finalize_binance_transport(
        project_dir=tmp_path,
        data_dir=data_dir,
        report=report,
        requirements=requirements,
        plan=plan,
    )
    assert outputs.funding_schedules[0].settlement_times == (window.start,)
    assert {item.role for item in outputs.market_data} == {"CANDLE_ONE_MINUTE"}
    assert prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=outputs.market_data,
        funding_schedules=outputs.funding_schedules,
    ).summary.can_start_dev_replay


def test_retry_is_bounded_and_terminal_failure_is_sticky(tmp_path: Path) -> None:
    report, requirements, plan = _inputs()
    funding_time = int(report.accepted_requests[0].request.start.timestamp() * 1000)
    calls: Counter[str] = Counter()
    with _client((funding_time,), calls, first_status=500) as client:
        result = run_binance_transport(
            project_dir=tmp_path,
            data_dir=tmp_path / "retry",
            plan=plan,
            approved_requirements_plan_hash=requirements.plan_hash,
            client=client,
            now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
            sleep=lambda _: None,
        )
    assert sum(calls.values()) == len(plan.tasks) + 1
    assert sum(item.attempts_completed for item in result.entries) == len(plan.tasks) + 1

    failed_calls: Counter[str] = Counter()
    with _client((funding_time,), failed_calls, first_status=400) as client:
        with pytest.raises(BinanceTransportError, match="failed after 1 attempt"):
            run_binance_transport(
                project_dir=tmp_path,
                data_dir=tmp_path / "failed",
                plan=plan,
                approved_requirements_plan_hash=requirements.plan_hash,
                client=client,
                now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
                sleep=lambda _: None,
            )
        first = sum(failed_calls.values())
        with pytest.raises(BinanceTransportError, match="previously failed"):
            run_binance_transport(
                project_dir=tmp_path,
                data_dir=tmp_path / "failed",
                plan=plan,
                approved_requirements_plan_hash=requirements.plan_hash,
                client=client,
                now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
                sleep=lambda _: None,
            )
    assert first == sum(failed_calls.values()) == 1


def test_wrong_owner_approval_makes_no_request(tmp_path: Path) -> None:
    _, _, plan = _inputs()
    calls: Counter[str] = Counter()
    with _client((0,), calls) as client:
        with pytest.raises(BinanceTransportError, match="owner approval"):
            run_binance_transport(
                project_dir=tmp_path,
                data_dir=tmp_path / "market",
                plan=plan,
                approved_requirements_plan_hash="0" * 64,
                client=client,
            )
    assert not calls
