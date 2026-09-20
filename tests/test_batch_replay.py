"""Synthetic checks for the recoverable P9 batch replay boundary."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from slagalpha.backtest.analytics import FundingDataset
from slagalpha.backtest.costs import COST_RATES, CostScenario
from slagalpha.backtest.replay import ArmedReplayRequest, TradeReplayRequest
from slagalpha.backtest.runner import replay_cases
from slagalpha.domain.universe import EvidenceConfidence, RegistryVerification
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research import batch_replay
from slagalpha.research.batch_replay import (
    BatchReplayCaseResult,
    BatchReplayInputError,
    plan_batch_replay_cases,
    run_replay_batch,
)
from slagalpha.research.parameters import (
    DevParameterVersion,
    build_dev_parameter_version,
)
from slagalpha.research.replay_inputs import DevReplayDataRequest, replay_data_bounds
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    ReplayRestResponse,
)
from slagalpha.research.replay_prep import ReplayReadyInput, ReplayReadyManifest
from slagalpha.research.sensitivity import (
    SensitivityPlan,
    default_candidates,
)
from slagalpha.research.splits import ResearchReadiness
from slagalpha.strategy.setup import Direction
from slagalpha.strategy.triggers import STRATEGY_VERSION


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _plan() -> SensitivityPlan:
    candidates = default_candidates()
    payload = {
        "schema_version": "sensitivity-plan/0.1.0",
        "strategy_version": STRATEGY_VERSION,
        "strategy_rules_sha256": _sha("rules"),
        "dataset_role": "DEV",
        "split_hash": _sha("split"),
        "input_audit_hash": _sha("audit"),
        "contract_registry_version": "synthetic-registry",
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "cost_rates": {
            scenario.value: rates.model_dump(mode="json")
            for scenario, rates in COST_RATES.items()
        },
        "planned_evaluation_count": len(candidates) * len(COST_RATES),
        "dev_input_readiness": ResearchReadiness.READY.value,
        "blockers": [],
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    return SensitivityPlan.model_validate({**payload, "plan_hash": _hash(payload)})


def _versions(plan: SensitivityPlan) -> tuple[DevParameterVersion, ...]:
    return tuple(
        build_dev_parameter_version(plan=plan, candidate_hash=item.candidate_hash)
        for item in plan.candidates
    )


def _request(
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    signal: str = "signal",
) -> DevReplayDataRequest:
    confirmation = datetime(2024, 1, 1, 10, tzinfo=UTC)
    replay_request = TradeReplayRequest(
        armed=ArmedReplayRequest(
            logical_signal_id=_sha(signal),
            symbol="AAAUSDT",
            direction=Direction.LONG,
            entry_price=Decimal("100"),
            invalidation_price=Decimal("90"),
            stop_price=Decimal("89"),
            atr_at_confirmation=Decimal("10"),
            confirmation_close=confirmation,
            expires_at=confirmation + timedelta(hours=1),
        ),
        tp1=Decimal("111"),
        tp2=Decimal("122"),
        tick_size=Decimal("0.1"),
        max_holding_bars=parameter.candidate.parameters.max_holding_bars,
    )
    start, end = replay_data_bounds(replay_request)
    payload = {
        "schema_version": "dev-replay-data-request/0.1.0",
        "dataset_role": "DEV",
        "split_hash": plan.split_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "parameter_content_hash": parameter.content_hash,
        "trade_plan_hash": _sha("trade-plan"),
        "universe_content_hash": _sha("universe"),
        "registry_content_hash": _sha("registry"),
        "rule_content_hash": _sha("rule"),
        "rule_verification_status": RegistryVerification.VERIFIED.value,
        "rule_confidence": EvidenceConfidence.HIGH.value,
        "rule_warning_codes": [],
        "request": replay_request.model_dump(mode="json"),
        "start": start.isoformat().replace("+00:00", "Z"),
        "end_exclusive": end.isoformat().replace("+00:00", "Z"),
        "expected_candle_count": int((end - start).total_seconds() / 60),
        "download_authorized": False,
        "research_authorized": False,
    }
    return DevReplayDataRequest.model_validate(
        {**payload, "request_hash": _hash(payload)}
    )


def _artifact(request: DevReplayDataRequest, seed: str) -> ReplayMarketDataArtifact:
    response = ReplayRestResponse(
        endpoint="/fapi/v1/klines",
        symbol=request.request.armed.symbol,
        interval="1m",
        start_time_ms=int(request.start.timestamp() * 1000),
        end_time_ms=int(request.end_exclusive.timestamp() * 1000) - 1,
        limit=1500,
        observed_at=request.end_exclusive,
        http_status=200,
        relative_path=f"synthetic/{seed}.json",
        body_sha256=_sha(f"body-{seed}"),
    )
    payload = {
        "schema_version": "replay-market-data/0.1.0",
        "role": "CANDLE_ONE_MINUTE",
        "request_hash": request.request_hash,
        "responses": [response.model_dump(mode="json")],
        "record_count": request.expected_candle_count,
        "normalized_content_hash": _sha(f"candles-{seed}"),
        "research_authorized": False,
    }
    return ReplayMarketDataArtifact.model_validate(
        {**payload, "artifact_hash": _hash(payload)}
    )


def _manifest(
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    seed: str = "one",
    signal: str = "signal",
) -> tuple[ReplayReadyManifest, ReplayMarketDataArtifact]:
    request = _request(plan, parameter, signal)
    artifact = _artifact(request, seed)
    market_payload = {
        "candle_artifact_hash": artifact.artifact_hash,
        "funding_artifact_hash": None,
        "funding_schedule_hash": _sha("schedule"),
    }
    ready_payload = {
        "schema_version": "p9-dev-replay-input/0.1.0",
        "dataset_role": "DEV",
        "source_scan_report_hash": _sha("scan"),
        "request_set_hash": _sha("request-set"),
        "request": request.model_dump(mode="json"),
        "request_hash": request.request_hash,
        "candle_artifact_hash": artifact.artifact_hash,
        "funding_required": False,
        "funding_artifact_hash": None,
        "funding_schedule_hash": _sha("schedule"),
        "required_funding_times": [],
        "market_data_hash": _hash(market_payload),
        "registry_content_hash": request.registry_content_hash,
        "rule_content_hash": request.rule_content_hash,
        "rule_verification_status": request.rule_verification_status.value,
        "rule_confidence": request.rule_confidence.value,
        "approximate_historical_rule_warnings": [],
        "cost_scenarios": [item.value for item in CostScenario],
        "validation_locked_test_authorized": False,
    }
    ready = ReplayReadyInput.model_validate(
        {**ready_payload, "input_hash": _hash(ready_payload)}
    )
    manifest_payload = {
        "schema_version": "p9-dev-replay-ready-manifest/0.1.0",
        "dataset_role": "DEV",
        "source_scan_report_hash": ready.source_scan_report_hash,
        "request_set_hash": ready.request_set_hash,
        "accepted_request_count": 1,
        "ready_request_count": 1,
        "inputs": [ready.model_dump(mode="json")],
        "validation_locked_test_authorized": False,
    }
    manifest = ReplayReadyManifest.model_validate(
        {**manifest_payload, "manifest_hash": _hash(manifest_payload)}
    )
    return manifest, artifact


def _candles(request: DevReplayDataRequest) -> pd.DataFrame:
    opens = pd.date_range(
        request.start, periods=request.expected_candle_count, freq="1min"
    )
    frame = pd.DataFrame(
        {
            "open_time": opens,
            "close_time_exclusive": opens + pd.Timedelta(minutes=1),
            "open": Decimal("100"),
            "high": Decimal("100"),
            "low": Decimal("100"),
            "close": Decimal("100"),
            "quote_volume": Decimal("1000"),
            "is_closed": True,
        }
    )
    frame.loc[0, ["open", "high", "low", "close"]] = [
        Decimal("99"),
        Decimal("101"),
        Decimal("98"),
        Decimal("100"),
    ]
    frame.loc[1, ["open", "high", "low", "close"]] = [
        Decimal("105"),
        Decimal("123"),
        Decimal("99"),
        Decimal("120"),
    ]
    return frame


@pytest.fixture
def synthetic_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    SensitivityPlan,
    tuple[DevParameterVersion, ...],
    ReplayReadyManifest,
    ReplayMarketDataArtifact,
    list[str],
]:
    plan = _plan()
    versions = _versions(plan)
    baseline = next(
        item for item in versions if item.candidate.changed_parameter == "BASELINE"
    )
    manifest, artifact = _manifest(plan, baseline)
    calls: list[str] = []

    def load_synthetic(
        *,
        project_dir: Path,
        request: DevReplayDataRequest,
        artifact: ReplayMarketDataArtifact,
    ) -> pd.DataFrame | FundingDataset:
        del project_dir
        calls.append(artifact.artifact_hash)
        return _candles(request)

    monkeypatch.setattr(
        batch_replay, "load_replay_market_data_artifact", load_synthetic
    )
    return plan, versions, manifest, artifact, calls


def test_batch_is_deterministic_and_complete_results_resume(
    tmp_path: Path,
    synthetic_batch: tuple[
        SensitivityPlan,
        tuple[DevParameterVersion, ...],
        ReplayReadyManifest,
        ReplayMarketDataArtifact,
        list[str],
    ],
) -> None:
    plan, versions, manifest, artifact, calls = synthetic_batch
    output = tmp_path / "output"
    checkpoint_path = output / "checkpoint.json"
    first = run_replay_batch(
        project_dir=tmp_path,
        manifest=manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(artifact,),
        results_dir=output,
        checkpoint_path=checkpoint_path,
    )
    assert first.status == "COMPLETE"
    assert len(first.entries) == 3
    assert len(calls) == 1
    assert len({item.result_hash for item in first.entries}) == 3

    fresh = run_replay_batch(
        project_dir=tmp_path,
        manifest=manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(artifact,),
        results_dir=tmp_path / "fresh",
        checkpoint_path=tmp_path / "fresh" / "checkpoint.json",
    )
    assert {
        item.case_id: item.result_hash for item in fresh.entries
    } == {item.case_id: item.result_hash for item in first.entries}
    assert len(calls) == 2

    repeated = run_replay_batch(
        project_dir=tmp_path,
        manifest=manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(artifact,),
        results_dir=output,
        checkpoint_path=checkpoint_path,
    )
    assert repeated == first
    assert len(calls) == 2

    result_path = output / "cases" / f"{first.entries[1].result_hash}.json"
    result = BatchReplayCaseResult.model_validate_json(result_path.read_bytes())
    assert result.gross_r is not None
    assert result.fees >= 0
    assert result.slippage >= 0
    assert result.funding == 0
    assert result.net_r is not None
    assert result.mae_r is not None and result.mfe_r is not None
    assert result.exit_reason == "TP2"
    assert result.ready_manifest_hash == manifest.manifest_hash
    assert result.funding_mark_supplement_hashes == ()
    assert result.funding_mark_warnings == ()


def test_failed_cases_are_not_skipped_and_resume_independently(
    tmp_path: Path,
    synthetic_batch: tuple[
        SensitivityPlan,
        tuple[DevParameterVersion, ...],
        ReplayReadyManifest,
        ReplayMarketDataArtifact,
        list[str],
    ],
) -> None:
    plan, versions, manifest, artifact, _ = synthetic_batch
    checkpoint_path = tmp_path / "checkpoint.json"
    failed = run_replay_batch(
        project_dir=tmp_path,
        manifest=manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(),
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    )
    assert failed.status == "FAILED"
    assert len(failed.entries) == 3
    assert all(item.state == "FAILED" for item in failed.entries)

    recovered = run_replay_batch(
        project_dir=tmp_path,
        manifest=manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(artifact,),
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    )
    assert recovered.status == "COMPLETE"
    assert all(item.state == "COMPLETE" for item in recovered.entries)


def test_partial_status_and_input_or_version_change_invalidate_checkpoint(
    tmp_path: Path,
    synthetic_batch: tuple[
        SensitivityPlan,
        tuple[DevParameterVersion, ...],
        ReplayReadyManifest,
        ReplayMarketDataArtifact,
        list[str],
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, versions, manifest, artifact, calls = synthetic_batch
    original_replay = replay_cases

    def fail_stress(*args: Any, **kwargs: Any) -> Any:
        scenario = args[0][0].scenario
        if scenario is CostScenario.STRESS:
            raise ValueError("synthetic cost failure")
        return original_replay(*args, **kwargs)

    monkeypatch.setattr(batch_replay, "replay_cases", fail_stress)
    checkpoint_path = tmp_path / "checkpoint.json"
    partial = run_replay_batch(
        project_dir=tmp_path,
        manifest=manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(artifact,),
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    )
    assert partial.status == "PARTIAL"
    assert sum(item.state == "COMPLETE" for item in partial.entries) == 2
    assert sum(item.state == "FAILED" for item in partial.entries) == 1

    monkeypatch.setattr(batch_replay, "replay_cases", original_replay)
    changed_manifest, changed_artifact = _manifest(
        plan,
        next(item for item in versions if item.candidate.changed_parameter == "BASELINE"),
        "changed",
    )
    prior_hashes = {item.result_hash for item in partial.entries if item.result_hash}
    changed_input = run_replay_batch(
        project_dir=tmp_path,
        manifest=changed_manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(changed_artifact,),
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    )
    assert changed_input.status == "COMPLETE"
    assert len(calls) == 2
    changed_input_hashes = {item.result_hash for item in changed_input.entries}
    assert prior_hashes.isdisjoint(changed_input_hashes)

    monkeypatch.setattr(
        batch_replay, "BATCH_REPLAY_VERSION", "p9-batch-replay/0.1.2"
    )
    changed_version = run_replay_batch(
        project_dir=tmp_path,
        manifest=changed_manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(changed_artifact,),
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    )
    assert changed_version.status == "COMPLETE"
    assert len(calls) == 3
    assert changed_input_hashes.isdisjoint(
        {item.result_hash for item in changed_version.entries}
    )


def test_batch_delegates_active_trade_exclusion_to_p7_scheduler(
    tmp_path: Path,
    synthetic_batch: tuple[
        SensitivityPlan,
        tuple[DevParameterVersion, ...],
        ReplayReadyManifest,
        ReplayMarketDataArtifact,
        list[str],
    ],
) -> None:
    plan, versions, first_manifest, first_artifact, _ = synthetic_batch
    baseline = next(
        item for item in versions if item.candidate.changed_parameter == "BASELINE"
    )
    second_manifest, second_artifact = _manifest(
        plan, baseline, seed="second", signal="second-signal"
    )
    inputs = tuple(sorted(
        (*first_manifest.inputs, *second_manifest.inputs),
        key=lambda item: item.request_hash,
    ))
    payload = {
        "schema_version": "p9-dev-replay-ready-manifest/0.1.0",
        "dataset_role": "DEV",
        "source_scan_report_hash": first_manifest.source_scan_report_hash,
        "request_set_hash": first_manifest.request_set_hash,
        "accepted_request_count": 2,
        "ready_request_count": 2,
        "inputs": [item.model_dump(mode="json") for item in inputs],
        "validation_locked_test_authorized": False,
    }
    manifest = ReplayReadyManifest.model_validate(
        {**payload, "manifest_hash": _hash(payload)}
    )
    checkpoint = run_replay_batch(
        project_dir=tmp_path,
        manifest=manifest,
        plan=plan,
        parameter_versions=versions,
        market_data=(first_artifact, second_artifact),
        results_dir=tmp_path,
        checkpoint_path=tmp_path / "checkpoint.json",
        cost_scenarios=(CostScenario.ZERO,),
    )
    results = tuple(
        BatchReplayCaseResult.model_validate_json(
            (tmp_path / "cases" / f"{entry.result_hash}.json").read_bytes()
        )
        for entry in checkpoint.entries
    )
    assert checkpoint.status == "COMPLETE"
    assert {item.scheduler_status.value for item in results} == {
        "EXECUTED",
        "SKIPPED_ACTIVE_TRADE",
    }
    skipped = next(
        item for item in results if item.scheduler_status.value == "SKIPPED_ACTIVE_TRADE"
    )
    assert skipped.blocked_by_logical_signal_id is not None
    assert skipped.p7_summary is None
    assert skipped.gross_r is skipped.net_r is None


def test_incomplete_ready_manifest_fails_closed() -> None:
    plan = _plan()
    versions = _versions(plan)
    payload = {
        "schema_version": "p9-dev-replay-ready-manifest/0.1.0",
        "dataset_role": "DEV",
        "source_scan_report_hash": _sha("scan"),
        "request_set_hash": _sha("requests"),
        "accepted_request_count": 1,
        "ready_request_count": 0,
        "inputs": [],
        "validation_locked_test_authorized": False,
    }
    manifest = ReplayReadyManifest.model_validate(
        {**payload, "manifest_hash": _hash(payload)}
    )
    with pytest.raises(BatchReplayInputError, match="no request may be skipped"):
        plan_batch_replay_cases(
            manifest=manifest,
            plan=plan,
            parameter_versions=versions,
        )
