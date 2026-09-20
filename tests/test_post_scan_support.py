"""Offline post-scan helpers; no scanner, replay, or network execution."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.backtest.costs import CostScenario
from slagalpha.data.download_cache import (
    DownloadCacheError,
    DownloadCheckpoint,
    DownloadProvenance,
    build_download_task,
    commit_staged_download,
)
from slagalpha.reporting.dev_research_summary import (
    CandidateResearchSummary,
    ScenarioResearchSummary,
    build_p9_dev_research_summary,
)
from slagalpha.reporting.dev_scan_summary import (
    build_dev_scan_summary,
    render_dev_scan_summary,
)
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.dev_source_scan import (
    AcceptedP6Request,
    DevMarketDataRequirement,
    DevScanFunnel,
    DevSourceScanReport,
)
from slagalpha.research.market_data_requirements import (
    MarketDataRequirementError,
    build_market_data_requirement_plan,
)
from slagalpha.research.request_set import build_dev_replay_request_set
from test_request_set import _request_set_context
from test_sensitivity_plan import _plan


def _scan_report(*, partial: bool = False) -> DevSourceScanReport:
    accepted_times = (
        datetime(2024, 1, 1, 0, 15, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 30, tzinfo=UTC),
    )
    context = _request_set_context(accepted_times=accepted_times)
    request_set = build_dev_replay_request_set(**context)
    accepted = tuple(
        AcceptedP6Request(request=request, approximate_tick_impact=None)
        for request in request_set.requests
    )
    request_payload = [item.model_dump(mode="json") for item in accepted]
    request_set_hash = hashlib.sha256(
        canonical_json_bytes({"accepted_requests": request_payload})
    ).hexdigest()
    funnel = DevScanFunnel(
        scan_slot_count=2,
        setup_evaluated_count=2,
        setup_not_ready_count=0,
        setup_eligible_count=2,
        trigger_evaluated_count=2,
        trigger_not_ready_count=0,
        trigger_confirmed_count=2,
        entry_stop_rejected_count=0,
        take_profit_rejected_count=0,
        request_boundary_rejected_count=0,
        accepted_count=2,
        source_error_slot_count=0,
    )
    merged_minutes = int(
        (accepted[1].request.end_exclusive - accepted[0].request.start).total_seconds() // 60
    )
    payload = {
        "schema_version": "dev-source-scan/0.1.0",
        "scan_plan_hash": "1" * 64,
        "split_hash": "2" * 64,
        "sensitivity_plan_hash": "3" * 64,
        "parameter_content_hash": "4" * 64,
        "registry_content_hash": "5" * 64,
        "source_inventory_hash": "6" * 64,
        "source_validation_mode": "BOUND_RAW_AND_PARQUET_SHA256",
        "source_partition_count": 4,
        "validated_source_partition_count": 4,
        "scan_start": accepted[0].request.start.isoformat().replace("+00:00", "Z"),
        "scan_end_exclusive": accepted[-1].request.end_exclusive.isoformat().replace(
            "+00:00", "Z"
        ),
        "universe_symbol_count": 1,
        "scanned_symbols": [accepted[0].request.request.armed.symbol],
        "accepted_symbols": [accepted[0].request.request.armed.symbol],
        "funnel": funnel.model_dump(mode="json"),
        "reason_counts": {"ACCEPTED": 2},
        "fallback_rule_slot_count": 0,
        "fallback_price_plan_count": 0,
        "approximate_price_observation_count": 0,
        "approximate_outcome_warning_count": 0,
        "max_approximate_tick_adjustment_bps": None,
        "warning_codes": [],
        "accepted_requests": request_payload,
        "request_set_hash": request_set_hash,
        "market_data_requirement": DevMarketDataRequirement(
            symbols=(accepted[0].request.request.armed.symbol,),
            one_minute_rows_before_overlap_dedup=sum(
                item.request.expected_candle_count for item in accepted
            ),
            one_minute_rows_after_overlap_dedup=merged_minutes,
            funding_windows_before_overlap_dedup=2,
            funding_windows_after_overlap_dedup=1,
            merged_window_minutes=merged_minutes,
        ).model_dump(mode="json"),
        "anomalies": ["BTCUSDT: synthetic source error"] if partial else [],
        "status": "PARTIAL" if partial else "COMPLETE",
        "download_authorized": False,
        "replay_executed": False,
        "locked_test_consumed": False,
    }
    return DevSourceScanReport.model_validate(
        {**payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}
    )


def test_human_summary_and_requirement_plan_merge_overlapping_requests() -> None:
    report = _scan_report()
    summary = build_dev_scan_summary(report)
    text = render_dev_scan_summary(summary)
    assert summary.completed_symbol_count == 1
    assert summary.accepted_count == 2
    assert "accepted 2" in text
    assert "未发现零结果" in text

    plan = build_market_data_requirement_plan(report)
    requirement = plan.requirements[0]
    assert len(requirement.windows) == 1
    assert plan.one_minute_rows_before_overlap_dedup == 1052
    assert plan.one_minute_rows_after_overlap_dedup == 541
    assert requirement.windows[0].funding_record_count_estimate is None
    assert plan.network_accessed is plan.download_authorized is False


def test_partial_report_can_be_summarized_but_not_planned() -> None:
    report = _scan_report(partial=True)
    assert "不能据此启动" in render_dev_scan_summary(build_dev_scan_summary(report))
    with pytest.raises(MarketDataRequirementError, match="partial scan"):
        build_market_data_requirement_plan(report)


def test_download_cache_checksums_deduplicates_and_keeps_provenance(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(b"public response")
    second.write_bytes(b"public response")
    task = build_download_task(
        provider="BINANCE_USDM",
        endpoint="/fapi/v1/klines",
        parameters=(("symbol", "BTCUSDT"), ("interval", "1m")),
    )
    provenance = DownloadProvenance(
        cache_key=task.cache_key,
        provider=task.provider,
        endpoint=task.endpoint,
        parameters=task.parameters,
        observed_at=datetime(2026, 9, 11, tzinfo=UTC),
        http_status=200,
        etag='"fixture"',
    )
    expected = hashlib.sha256(first.read_bytes()).hexdigest()
    initial = commit_staged_download(
        staged_path=first,
        cache_dir=tmp_path / "cache",
        provenance=provenance,
        expected_sha256=expected,
    )
    duplicate = commit_staged_download(
        staged_path=second,
        cache_dir=tmp_path / "cache",
        provenance=provenance,
        expected_sha256=expected,
    )
    assert initial.duplicate_content is False
    assert duplicate.duplicate_content is True
    assert initial.content_sha256 == duplicate.content_sha256 == expected
    with pytest.raises(DownloadCacheError, match="checksum mismatch"):
        commit_staged_download(
            staged_path=first,
            cache_dir=tmp_path / "other-cache",
            provenance=provenance,
            expected_sha256="0" * 64,
        )


def test_resume_requires_validator_and_retry_budget_reconciles() -> None:
    values = {
        "cache_key": "a" * 64,
        "attempts_completed": 1,
        "max_attempts": 5,
        "bytes_received": 100,
        "state": "PARTIAL",
    }
    with pytest.raises(ValidationError, match="resume requires"):
        DownloadCheckpoint.model_validate(values)
    checkpoint = DownloadCheckpoint.model_validate({**values, "etag": '"v1"'})
    assert checkpoint.bytes_received == 100
    with pytest.raises(ValidationError, match="consume the retry budget"):
        DownloadCheckpoint.model_validate({**values, "bytes_received": 0, "state": "EXHAUSTED"})


def test_provenance_must_match_canonical_request_identity() -> None:
    with pytest.raises(ValidationError, match="cache key"):
        DownloadProvenance(
            cache_key="a" * 64,
            provider="BINANCE_USDM",
            endpoint="/fapi/v1/fundingRate",
            parameters=(("symbol", "BTCUSDT"),),
            observed_at=datetime(2026, 9, 11, tzinfo=UTC),
            http_status=200,
        )


def _scenario(scenario: CostScenario, requests: int = 2) -> ScenarioResearchSummary:
    return ScenarioResearchSummary(
        scenario=scenario,
        request_count=requests,
        executed_count=requests,
        skipped_active_count=0,
        closed_trade_count=requests,
        net_eligible_count=requests,
        winner_count=1,
        win_rate=Decimal("0.5"),
        total_net_r=Decimal("0.4"),
        expectancy_net_r=Decimal("0.2"),
        max_drawdown_r=Decimal("-0.3"),
        total_fee=Decimal("0.01"),
        total_slippage_cost=Decimal("0.02"),
        total_funding_cash_flow=Decimal("-0.001"),
        data_warning_count=0,
    )


def test_research_summary_requires_exact_frozen_ten_by_three_matrix() -> None:
    plan = _plan()
    candidates = tuple(
        CandidateResearchSummary(
            candidate=candidate,
            scenarios=tuple(_scenario(scenario) for scenario in CostScenario),
        )
        for candidate in plan.candidates
    )
    summary = build_p9_dev_research_summary(
        plan=plan,
        source_scan_report_hash="7" * 64,
        request_set_hash="8" * 64,
        candidates=candidates,
    )
    assert summary.candidate_count == 10
    assert summary.scenario_count == 3
    assert summary.evaluation_count == 30
    with pytest.raises(ValueError, match="frozen candidate plan"):
        build_p9_dev_research_summary(
            plan=plan,
            source_scan_report_hash="7" * 64,
            request_set_hash="8" * 64,
            candidates=tuple(reversed(candidates)),
        )
