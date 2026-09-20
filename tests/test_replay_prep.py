"""Offline synthetic coverage for P9 replay preparation and recovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from slagalpha.data.binance_usdm_transport import build_binance_transport_plan
from slagalpha.reporting.run_manifest import RunManifestError, canonical_json_bytes
from slagalpha.research.market_data_requirements import build_market_data_requirement_plan
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    ReplayRestResponse,
    build_replay_market_data_artifact,
)
from slagalpha.research.replay_prep import (
    FundingScheduleArtifact,
    build_funding_schedule_artifact,
    build_resume_plan,
    load_replay_checkpoint,
    prepare_dev_replay,
    render_replay_readiness,
    update_replay_checkpoint,
    write_replay_checkpoint,
    write_replay_resume_plan,
)
from test_post_scan_support import _scan_report
from test_replay_market_data import _funding, _klines, _response


def _rehash_artifact(
    artifact: ReplayMarketDataArtifact, response: ReplayRestResponse
) -> ReplayMarketDataArtifact:
    payload = artifact.model_dump(mode="json", exclude={"artifact_hash"})
    payload["responses"] = [response.model_dump(mode="json")]
    return ReplayMarketDataArtifact.model_validate(
        {
            **payload,
            "artifact_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        }
    )


def _replace_body(
    root: Path,
    artifact: ReplayMarketDataArtifact,
    body: Any,
    *,
    change_response: dict[str, Any] | None = None,
) -> ReplayMarketDataArtifact:
    response = artifact.responses[0]
    content = json.dumps(body).encode()
    (root / response.relative_path).write_bytes(content)
    response = response.model_copy(
        update={"body_sha256": hashlib.sha256(content).hexdigest(), **(change_response or {})}
    )
    return _rehash_artifact(artifact, response)


def _fixture(
    root: Path,
    *,
    funding_required: bool = True,
) -> tuple[
    Any,
    tuple[ReplayMarketDataArtifact, ...],
    tuple[FundingScheduleArtifact, ...],
]:
    root.mkdir(parents=True, exist_ok=True)
    report = _scan_report()
    artifacts: list[ReplayMarketDataArtifact] = []
    settlement = report.accepted_requests[0].request.start + timedelta(hours=2)
    for index, accepted in enumerate(report.accepted_requests):
        request = accepted.request
        candle_response = _response(
            root, request, _klines(request), name=f"candle-{index}.json"
        )
        candle, _ = build_replay_market_data_artifact(
            project_dir=root,
            request=request,
            role="CANDLE_ONE_MINUTE",
            responses=(candle_response,),
        )
        artifacts.append(candle)
        if funding_required:
            rows = [{**_funding(request)[0], "fundingTime": int(settlement.timestamp() * 1000)}]
            funding_response = _response(
                root,
                request,
                rows,
                endpoint="/fapi/v1/fundingRate",
                name=f"funding-{index}.json",
            )
            funding, _ = build_replay_market_data_artifact(
                project_dir=root,
                request=request,
                role="FUNDING",
                responses=(funding_response,),
            )
            artifacts.append(funding)
    first = report.accepted_requests[0].request
    last = report.accepted_requests[-1].request
    schedule = build_funding_schedule_artifact(
        symbol=first.request.armed.symbol,
        coverage_start=first.start,
        coverage_end_exclusive=last.end_exclusive,
        settlement_times=(settlement,) if funding_required else (),
        provider="BINANCE_USDM",
        schedule_version="synthetic-v1",
        source_ref="fixture://funding-schedule",
        source_content_sha256="9" * 64,
        observed_at=last.end_exclusive + timedelta(days=1),
    )
    return report, tuple(artifacts), (schedule,)


def _first(
    artifacts: tuple[ReplayMarketDataArtifact, ...],
    role: Literal["CANDLE_ONE_MINUTE", "FUNDING"],
) -> ReplayMarketDataArtifact:
    return next(item for item in artifacts if item.role == role)


def _replace(
    artifacts: tuple[ReplayMarketDataArtifact, ...], changed: ReplayMarketDataArtifact
) -> tuple[ReplayMarketDataArtifact, ...]:
    return tuple(
        changed
        if item.request_hash == changed.request_hash and item.role == changed.role
        else item
        for item in artifacts
    )


def test_complete_inputs_are_stable_and_human_readable(tmp_path: Path) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    first = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    )
    second = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    )
    assert first == second
    assert first.summary.can_start_dev_replay is True
    assert first.summary.replay_ready_count == first.summary.accepted_request_count == 2
    assert first.summary.not_ready_request_count == 0
    assert all(item.funding_required for item in first.manifest.inputs)
    assert all(
        item.request_hash == item.request.request_hash for item in first.manifest.inputs
    )
    assert all(
        item.cost_scenarios == ("ZERO", "BASELINE", "STRESS")
        for item in first.manifest.inputs
    )
    assert "可正式启动 DEV replay：是" in render_replay_readiness(first.summary)


@pytest.mark.parametrize("missing_count", [1, 3])
def test_missing_candle_minutes_fail_closed(tmp_path: Path, missing_count: int) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    candle = _first(artifacts, "CANDLE_ONE_MINUTE")
    request = report.accepted_requests[0].request
    changed = _replace_body(tmp_path, candle, _klines(request)[:-missing_count])
    result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, changed),
        funding_schedules=schedules,
    )
    assert result.summary.can_start_dev_replay is False
    assert result.summary.candle_missing_blocked_count == 1
    assert result.summary.replay_ready_count == 1


@pytest.mark.parametrize("kind", ["duplicate", "disorder"])
def test_duplicate_and_disordered_candles_fail_closed(tmp_path: Path, kind: str) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    candle = _first(artifacts, "CANDLE_ONE_MINUTE")
    request = report.accepted_requests[0].request
    rows = _klines(request)
    if kind == "duplicate":
        rows[10] = rows[9]
    else:
        rows[9], rows[10] = rows[10], rows[9]
    changed = _replace_body(tmp_path, candle, rows)
    result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, changed),
        funding_schedules=schedules,
    )
    assert result.summary.duplicate_disorder_boundary_blocked_count == 1


def test_candle_symbol_utc_boundary_hash_and_provenance_fail_closed(tmp_path: Path) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    candle = _first(artifacts, "CANDLE_ONE_MINUTE")

    wrong_symbol = candle.responses[0].model_copy(update={"symbol": "OTHERUSDT"})
    symbol_result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, _rehash_artifact(candle, wrong_symbol)),
        funding_schedules=schedules,
    )
    assert symbol_result.summary.symbol_utc_blocked_count == 1

    wrong_boundary = candle.responses[0].model_copy(
        update={"start_time_ms": candle.responses[0].start_time_ms + 1}
    )
    boundary_result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, _rehash_artifact(candle, wrong_boundary)),
        funding_schedules=schedules,
    )
    assert boundary_result.summary.duplicate_disorder_boundary_blocked_count == 1

    (tmp_path / candle.responses[0].relative_path).write_bytes(b"changed")
    hash_result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    )
    assert hash_result.summary.hash_provenance_blocked_count == 1

    (tmp_path / candle.responses[0].relative_path).unlink()
    provenance_result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    )
    assert provenance_result.summary.hash_provenance_blocked_count == 1


def test_funding_is_required_only_when_possible_holding_crosses_schedule(
    tmp_path: Path,
) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    without_funding = tuple(item for item in artifacts if item.role != "FUNDING")
    blocked = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=without_funding,
        funding_schedules=schedules,
    )
    assert blocked.summary.funding_missing_blocked_count == 2

    no_funding_report, candle_only, empty_schedule = _fixture(
        tmp_path / "no-funding", funding_required=False
    )
    ready = prepare_dev_replay(
        project_dir=tmp_path / "no-funding",
        report=no_funding_report,
        market_data=candle_only,
        funding_schedules=empty_schedule,
    )
    assert ready.summary.can_start_dev_replay is True
    assert all(not item.funding_required for item in ready.manifest.inputs)


def test_required_funding_mark_price_missing_fails_closed(tmp_path: Path) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    funding = _first(artifacts, "FUNDING")
    request = report.accepted_requests[0].request
    rows = _funding(request)
    rows[0]["markPrice"] = ""
    response = funding.responses[0]
    content = json.dumps(rows).encode()
    (tmp_path / response.relative_path).write_bytes(content)
    changed_response = response.model_copy(
        update={"body_sha256": hashlib.sha256(content).hexdigest()}
    )
    changed, _ = build_replay_market_data_artifact(
        project_dir=tmp_path,
        request=request,
        role="FUNDING",
        responses=(changed_response,),
    )
    result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, changed),
        funding_schedules=schedules,
    )
    assert result.summary.can_start_dev_replay is False
    assert result.summary.replay_ready_count == 1
    assert result.summary.funding_missing_blocked_count == 1
    assert result.summary.funding_mark_price_missing_request_count == 1
    assert result.summary.funding_mark_price_missing_event_count == 1
    issue = next(
        item for item in result.summary.issues if item.request_hash == request.request_hash
    )
    assert issue.code == "FUNDING_MARK_PRICE_MISSING"
    assert "2024-01-01T02:15:00Z" in issue.message


def test_nonrequired_boundary_mark_price_is_not_a_blocker(tmp_path: Path) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    funding = _first(artifacts, "FUNDING")
    request = report.accepted_requests[0].request
    rows = _funding(request)
    rows.insert(
        0,
        {
            **rows[0],
            "fundingTime": int(request.start.timestamp() * 1000),
            "markPrice": "",
        },
    )
    response = funding.responses[0]
    content = json.dumps(rows).encode()
    (tmp_path / response.relative_path).write_bytes(content)
    changed_response = response.model_copy(
        update={"body_sha256": hashlib.sha256(content).hexdigest()}
    )
    changed, _ = build_replay_market_data_artifact(
        project_dir=tmp_path,
        request=request,
        role="FUNDING",
        responses=(changed_response,),
    )
    result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, changed),
        funding_schedules=schedules,
    )
    assert result.summary.not_ready_request_count == 0
    assert result.summary.funding_mark_price_missing_event_count == 0
    assert result.summary.can_start_dev_replay is True


@pytest.mark.parametrize("kind", ["duplicate", "disorder"])
def test_funding_duplicate_and_disorder_fail_closed(tmp_path: Path, kind: str) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    funding = _first(artifacts, "FUNDING")
    request = report.accepted_requests[0].request
    rows = _funding(request)
    second = {**rows[0], "fundingTime": rows[0]["fundingTime"] + 1}
    rows = [rows[0], rows[0]] if kind == "duplicate" else [second, rows[0]]
    changed = _replace_body(tmp_path, funding, rows)
    result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, changed),
        funding_schedules=schedules,
    )
    assert result.summary.can_start_dev_replay is False
    assert result.summary.duplicate_disorder_boundary_blocked_count == 1


def test_checkpoint_resume_invalidates_changed_input_and_never_repeats_complete(
    tmp_path: Path,
) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    manifest = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    ).manifest
    first_hash, second_hash = (item.request_hash for item in manifest.inputs)
    checkpoint = update_replay_checkpoint(
        manifest=manifest,
        checkpoint=None,
        request_hash=first_hash,
        result_hash="a" * 64,
    )
    assert checkpoint.status == "PARTIAL"
    resume = build_resume_plan(manifest, checkpoint)
    assert resume.completed_request_hashes == (first_hash,)
    assert resume.pending_request_hashes == (second_hash,)

    checkpoint = update_replay_checkpoint(
        manifest=manifest,
        checkpoint=checkpoint,
        request_hash=second_hash,
        result_hash="b" * 64,
    )
    assert checkpoint.status == "COMPLETE"
    assert build_resume_plan(manifest, checkpoint).pending_request_hashes == ()

    changed_candle = next(
        item
        for item in artifacts
        if item.role == "CANDLE_ONE_MINUTE" and item.request_hash == first_hash
    )
    response = changed_candle.responses[0].model_copy(
        update={"relative_path": "equivalent-candle.json"}
    )
    source = tmp_path / changed_candle.responses[0].relative_path
    (tmp_path / response.relative_path).write_bytes(source.read_bytes())
    changed_artifacts = _replace(artifacts, _rehash_artifact(changed_candle, response))
    changed_manifest = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=changed_artifacts,
        funding_schedules=schedules,
    ).manifest
    changed_resume = build_resume_plan(changed_manifest, checkpoint)
    assert changed_resume.invalidated_request_hashes == (first_hash,)
    assert changed_resume.pending_request_hashes == (first_hash,)
    assert changed_resume.completed_request_hashes == (second_hash,)

    metadata_payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    metadata_payload["accepted_request_count"] = 3
    metadata_manifest = changed_manifest.model_validate(
        {
            **metadata_payload,
            "manifest_hash": hashlib.sha256(canonical_json_bytes(metadata_payload)).hexdigest(),
        }
    )
    metadata_resume = build_resume_plan(metadata_manifest, checkpoint)
    assert metadata_resume.completed_request_hashes == ()
    assert metadata_resume.pending_request_hashes == (first_hash, second_hash)
    assert metadata_resume.invalidated_request_hashes == (first_hash, second_hash)


def test_checkpoint_records_failures_and_final_failed_state(tmp_path: Path) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    manifest = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    ).manifest
    checkpoint = None
    for item in manifest.inputs:
        checkpoint = update_replay_checkpoint(
            manifest=manifest,
            checkpoint=checkpoint,
            request_hash=item.request_hash,
            failure_reason="synthetic interruption",
        )
    assert checkpoint is not None
    assert checkpoint.status == "FAILED"
    assert all(item.failure_reason == "synthetic interruption" for item in checkpoint.entries)
    path = tmp_path / "checkpoint.json"
    write_replay_checkpoint(checkpoint, path)
    assert load_replay_checkpoint(path) == checkpoint


def test_cli_writes_json_text_manifest_and_inputs_offline(tmp_path: Path) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    report_path = tmp_path / "scan-report.json"
    market_dir = tmp_path / "market"
    schedule_dir = tmp_path / "schedules"
    output_dir = tmp_path / "output"
    checkpoint_path = tmp_path / "checkpoint.json"
    market_dir.mkdir()
    schedule_dir.mkdir()
    report_path.write_bytes(canonical_json_bytes(report.model_dump(mode="json")))
    for item in artifacts:
        (market_dir / f"{item.artifact_hash}.json").write_bytes(
            canonical_json_bytes(item.model_dump(mode="json"))
        )
    for schedule in schedules:
        (schedule_dir / f"{schedule.schedule_hash}.json").write_bytes(
            canonical_json_bytes(schedule.model_dump(mode="json"))
        )
    manifest = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    ).manifest
    checkpoint = update_replay_checkpoint(
        manifest=manifest,
        checkpoint=None,
        request_hash=manifest.inputs[0].request_hash,
        result_hash="a" * 64,
    )
    write_replay_checkpoint(checkpoint, checkpoint_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "p9_replay_preflight.py"),
            str(report_path),
            "--project-dir",
            str(tmp_path),
            "--market-data-dir",
            str(market_dir),
            "--funding-schedule-dir",
            str(schedule_dir),
            "--output-dir",
            str(output_dir),
            "--checkpoint",
            str(checkpoint_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert len(tuple(output_dir.glob("*.readiness.json"))) == 1
    assert len(tuple(output_dir.glob("*.readiness.txt"))) == 1
    assert len(tuple(output_dir.glob("*.replay-ready-manifest.json"))) == 1
    assert len(tuple(output_dir.glob("*.resume-plan.json"))) == 1
    assert len(tuple((output_dir / "inputs").glob("*.json"))) == 2


def test_authenticated_report_requirements_and_transport_bindings_are_retained(
    tmp_path: Path,
) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    requirements = build_market_data_requirement_plan(report)
    transport_plan = build_binance_transport_plan(
        report=report, requirements=requirements
    )
    result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
        requirements=requirements,
        transport_plan=transport_plan,
    )
    assert result.summary.requirements_plan_hash == requirements.plan_hash
    assert result.summary.transport_plan_hash == transport_plan.transport_plan_hash
    assert result.summary.can_start_dev_replay is True

    invalid_transport = transport_plan.model_copy(
        update={"requirements_plan_hash": "0" * 64}
    )
    blocked = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
        requirements=requirements,
        transport_plan=invalid_transport,
    )
    assert blocked.summary.can_start_dev_replay is False
    assert blocked.summary.hash_provenance_blocked_count == 2
    assert any(item.code == "INPUT_BINDING_MISMATCH" for item in blocked.summary.issues)


def test_resume_plan_publish_is_repeatable_fail_closed_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    manifest = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=artifacts,
        funding_schedules=schedules,
    ).manifest
    plan = build_resume_plan(manifest, None)

    destination = write_replay_resume_plan(plan, tmp_path / "first")
    original = destination.read_bytes()
    assert write_replay_resume_plan(plan, tmp_path / "first") == destination
    assert destination.read_bytes() == original

    conflict = tmp_path / "conflict" / destination.name
    conflict.parent.mkdir()
    conflict.write_bytes(b"conflict")
    with pytest.raises(RunManifestError, match="existing run artifact changed"):
        write_replay_resume_plan(plan, conflict.parent)
    assert conflict.read_bytes() == b"conflict"

    def fail_link(source: Path, target: Path) -> None:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr("slagalpha.reporting.run_manifest.os.link", fail_link)
    failed_dir = tmp_path / "failed"
    with pytest.raises(OSError, match="synthetic publish failure"):
        write_replay_resume_plan(plan, failed_dir)
    assert not (failed_dir / destination.name).exists()
    assert not tuple(failed_dir.glob("*.part"))
