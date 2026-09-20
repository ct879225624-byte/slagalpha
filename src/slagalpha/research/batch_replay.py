"""Recoverable, manifest-driven P9 DEV replay batches over frozen P7 semantics."""

from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.backtest.analytics import (
    FundingDataset,
    apply_funding,
    calculate_excursions,
)
from slagalpha.backtest.costs import COST_RATES, CostedReplayResult, CostScenario
from slagalpha.backtest.replay import REPLAY_VERSION
from slagalpha.backtest.runner import ReplayCase, ReplayCaseStatus, replay_cases
from slagalpha.backtest.summary import CanonicalReplaySummary, build_canonical_summary
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.funding_mark_supplement import (
    FundingMarkSupplementArtifact,
    verify_funding_mark_supplement,
)
from slagalpha.research.parameters import (
    DevParameterVersion,
    require_parameter_plan_binding,
)
from slagalpha.research.replay_inputs import ReplayDataInputError, Sha256
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    load_replay_market_data_artifact,
)
from slagalpha.research.replay_prep import ReplayReadyInput, ReplayReadyManifest
from slagalpha.research.sensitivity import SensitivityPlan

BATCH_REPLAY_VERSION = "p9-batch-replay/0.1.1"


class BatchReplayInputError(ValueError):
    """Raised when a batch cannot prove its complete DEV input scope."""


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _case_id(request_hash: str, candidate_hash: str, scenario: CostScenario) -> str:
    return _hash(
        {
            "candidate_hash": candidate_hash,
            "request_hash": request_hash,
            "scenario": scenario.value,
        }
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class BatchReplayCaseSpec(BaseModel):
    """One candidate-bound replay request under one frozen cost scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: Sha256
    execution_hash: Sha256
    request_hash: Sha256
    input_hash: Sha256
    candidate_hash: Sha256
    parameter_content_hash: Sha256
    scenario: CostScenario

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.case_id != _case_id(self.request_hash, self.candidate_hash, self.scenario):
            raise ValueError("batch replay case identity mismatch")
        return self


class BatchReplayCaseResult(BaseModel):
    """Immutable P7 result and the provenance needed to reproduce it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-batch-replay-result/0.2.0"] = "p9-batch-replay-result/0.2.0"
    runner_version: str
    replay_version: str
    case_id: Sha256
    execution_hash: Sha256
    request_hash: Sha256
    input_hash: Sha256
    ready_manifest_hash: Sha256
    source_scan_report_hash: Sha256
    request_set_hash: Sha256
    sensitivity_plan_hash: Sha256
    candidate_hash: Sha256
    parameter_content_hash: Sha256
    scenario: CostScenario
    candle_artifact_hash: Sha256
    funding_artifact_hash: Sha256 | None
    funding_schedule_hash: Sha256
    market_data_hash: Sha256
    registry_content_hash: Sha256
    rule_content_hash: Sha256
    rule_verification_status: str
    rule_confidence: str
    approximate_historical_rule_warnings: tuple[str, ...]
    funding_mark_supplement_hashes: tuple[Sha256, ...]
    funding_mark_warnings: tuple[str, ...]
    gross_r: Decimal | None
    fees: Decimal
    slippage: Decimal
    funding: Decimal
    net_r: Decimal | None
    mae_r: Decimal | None
    mfe_r: Decimal | None
    exit_reason: str | None
    scheduler_status: ReplayCaseStatus
    blocked_by_logical_signal_id: str | None
    p7_summary: CanonicalReplaySummary | None
    validation_locked_test_authorized: Literal[False] = False
    result_hash: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.runner_version != BATCH_REPLAY_VERSION:
            raise ValueError("unsupported batch replay runner version")
        if self.replay_version != REPLAY_VERSION:
            raise ValueError("unsupported P7 replay version")
        if self.case_id != _case_id(self.request_hash, self.candidate_hash, self.scenario):
            raise ValueError("batch result case identity mismatch")
        if (self.scheduler_status is ReplayCaseStatus.EXECUTED) != (self.p7_summary is not None):
            raise ValueError("scheduler status and P7 result disagree")
        if (self.scheduler_status is ReplayCaseStatus.SKIPPED_ACTIVE_TRADE) != (
            self.blocked_by_logical_signal_id is not None
        ):
            raise ValueError("active-trade skip provenance is incomplete")
        if (
            self.p7_summary is not None
            and hashlib.sha256(self.p7_summary.canonical_json.encode()).hexdigest()
            != self.p7_summary.canonical_hash
        ):
            raise ValueError("P7 canonical summary hash mismatch")
        payload = self.model_dump(mode="json", exclude={"result_hash"})
        if self.result_hash != _hash(payload):
            raise ValueError("batch replay result content hash mismatch")
        return self


class BatchReplayCheckpointEntry(BaseModel):
    """Resume state for exactly one case, never an execution result itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: Sha256
    execution_hash: Sha256
    request_hash: Sha256
    candidate_hash: Sha256
    scenario: CostScenario
    state: Literal["COMPLETE", "FAILED"]
    result_hash: Sha256 | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.case_id != _case_id(self.request_hash, self.candidate_hash, self.scenario):
            raise ValueError("checkpoint case identity mismatch")
        if self.state == "COMPLETE" and (
            self.result_hash is None or self.failure_reason is not None
        ):
            raise ValueError("completed case requires only a result hash")
        if self.state == "FAILED" and (self.result_hash is not None or not self.failure_reason):
            raise ValueError("failed case requires only an explicit reason")
        return self


class BatchReplayCheckpoint(BaseModel):
    """Crash-safe aggregate of independently resumable case states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-batch-replay-checkpoint/0.1.0"] = "p9-batch-replay-checkpoint/0.1.0"
    runner_version: str
    ready_manifest_hash: Sha256
    sensitivity_plan_hash: Sha256
    candidate_hashes: tuple[Sha256, ...]
    cost_scenarios: tuple[CostScenario, ...]
    expected_case_count: int = Field(gt=0, strict=True)
    entries: tuple[BatchReplayCheckpointEntry, ...]
    status: Literal["COMPLETE", "FAILED", "PARTIAL"]
    checkpoint_hash: Sha256

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        if self.candidate_hashes != tuple(sorted(set(self.candidate_hashes))):
            raise ValueError("checkpoint candidates must be unique and canonical")
        expected_scenarios = tuple(
            scenario for scenario in CostScenario if scenario in self.cost_scenarios
        )
        if not self.cost_scenarios or self.cost_scenarios != expected_scenarios:
            raise ValueError("checkpoint cost scenarios must be unique and canonical")
        case_ids = tuple(item.case_id for item in self.entries)
        if case_ids != tuple(sorted(set(case_ids))) or len(self.entries) > (
            self.expected_case_count
        ):
            raise ValueError("checkpoint entries must be unique, canonical, and bounded")
        complete = sum(item.state == "COMPLETE" for item in self.entries)
        failed = sum(item.state == "FAILED" for item in self.entries)
        expected_status = (
            "COMPLETE"
            if complete == self.expected_case_count
            else "FAILED"
            if failed == self.expected_case_count
            else "PARTIAL"
        )
        if self.status != expected_status:
            raise ValueError("checkpoint status does not reconcile")
        payload = self.model_dump(mode="json", exclude={"checkpoint_hash"})
        if self.checkpoint_hash != _hash(payload):
            raise ValueError("checkpoint content hash mismatch")
        return self


def _parameter_index(
    plan: SensitivityPlan,
    parameter_versions: tuple[DevParameterVersion, ...],
) -> dict[str, DevParameterVersion]:
    versions = tuple(
        DevParameterVersion.model_validate(item.model_dump(mode="json"))
        for item in parameter_versions
    )
    for version in versions:
        require_parameter_plan_binding(version, plan)
    by_content = {item.content_hash: item for item in versions}
    if len(by_content) != len(versions):
        raise BatchReplayInputError("parameter versions must not be duplicated")
    expected_candidates = {item.candidate_hash for item in plan.candidates}
    observed_candidates = {item.candidate.candidate_hash for item in versions}
    if observed_candidates != expected_candidates or len(versions) != len(plan.candidates):
        raise BatchReplayInputError("all ten frozen candidate versions are required")
    return by_content


def plan_batch_replay_cases(
    *,
    manifest: ReplayReadyManifest,
    plan: SensitivityPlan,
    parameter_versions: tuple[DevParameterVersion, ...],
    candidate_hashes: tuple[str, ...] | None = None,
    cost_scenarios: tuple[CostScenario, ...] | None = None,
) -> tuple[BatchReplayCaseSpec, ...]:
    """Bind ready requests to their original candidate and expand frozen costs."""

    manifest = ReplayReadyManifest.model_validate(manifest.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    if manifest.accepted_request_count == 0:
        raise BatchReplayInputError("ready manifest has no accepted DEV requests")
    if manifest.ready_request_count != manifest.accepted_request_count:
        raise BatchReplayInputError("ready manifest is incomplete; no request may be skipped")
    by_content = _parameter_index(plan, parameter_versions)
    supplied_candidates = tuple(candidate_hashes or ())
    if supplied_candidates and len(supplied_candidates) != len(set(supplied_candidates)):
        raise BatchReplayInputError("batch scope contains duplicate candidates")
    requested_candidates = (
        {item.candidate_hash for item in plan.candidates}
        if candidate_hashes is None
        else set(supplied_candidates)
    )
    known_candidates = {item.candidate_hash for item in plan.candidates}
    if not requested_candidates or not requested_candidates <= known_candidates:
        raise BatchReplayInputError("batch scope contains an unknown or empty candidate set")
    supplied_scenarios = cost_scenarios or tuple(CostScenario)
    if len(supplied_scenarios) != len(set(supplied_scenarios)):
        raise BatchReplayInputError("batch scope contains duplicate cost scenarios")
    requested_scenarios = (
        tuple(CostScenario)
        if cost_scenarios is None
        else tuple(scenario for scenario in CostScenario if scenario in cost_scenarios)
    )
    if not requested_scenarios:
        raise BatchReplayInputError("batch scope contains an empty cost scenario set")

    specs: list[BatchReplayCaseSpec] = []
    for ready in manifest.inputs:
        request = ready.request
        if request.sensitivity_plan_hash != plan.plan_hash:
            raise BatchReplayInputError("ready request belongs to another sensitivity plan")
        parameter = by_content.get(request.parameter_content_hash)
        if parameter is None:
            raise BatchReplayInputError("ready request has no frozen candidate version")
        candidate = parameter.candidate
        if request.request.max_holding_bars != candidate.parameters.max_holding_bars:
            raise BatchReplayInputError("ready P7 request changed its candidate holding period")
        if candidate.candidate_hash not in requested_candidates:
            continue
        for scenario in requested_scenarios:
            execution_payload = {
                "runner_version": BATCH_REPLAY_VERSION,
                "replay_version": REPLAY_VERSION,
                "ready_manifest_hash": manifest.manifest_hash,
                "sensitivity_plan_hash": plan.plan_hash,
                "input_hash": ready.input_hash,
                "parameter_content_hash": parameter.content_hash,
                "candidate_hash": candidate.candidate_hash,
                "scenario": scenario.value,
                "cost_rates": COST_RATES[scenario].model_dump(mode="json"),
            }
            specs.append(
                BatchReplayCaseSpec(
                    case_id=_case_id(ready.request_hash, candidate.candidate_hash, scenario),
                    execution_hash=_hash(execution_payload),
                    request_hash=ready.request_hash,
                    input_hash=ready.input_hash,
                    candidate_hash=candidate.candidate_hash,
                    parameter_content_hash=parameter.content_hash,
                    scenario=scenario,
                )
            )
    if not specs:
        raise BatchReplayInputError("batch scope has no candidate-bound ready requests")
    return tuple(sorted(specs, key=lambda item: item.case_id))


def _checkpoint(
    *,
    manifest: ReplayReadyManifest,
    plan: SensitivityPlan,
    specs: tuple[BatchReplayCaseSpec, ...],
    entries: tuple[BatchReplayCheckpointEntry, ...],
) -> BatchReplayCheckpoint:
    ordered = tuple(sorted(entries, key=lambda item: item.case_id))
    complete = sum(item.state == "COMPLETE" for item in ordered)
    failed = sum(item.state == "FAILED" for item in ordered)
    status = (
        "COMPLETE" if complete == len(specs) else "FAILED" if failed == len(specs) else "PARTIAL"
    )
    payload = {
        "schema_version": "p9-batch-replay-checkpoint/0.1.0",
        "runner_version": BATCH_REPLAY_VERSION,
        "ready_manifest_hash": manifest.manifest_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "candidate_hashes": sorted({item.candidate_hash for item in specs}),
        "cost_scenarios": [
            scenario.value
            for scenario in CostScenario
            if any(item.scenario is scenario for item in specs)
        ],
        "expected_case_count": len(specs),
        "entries": [item.model_dump(mode="json") for item in ordered],
        "status": status,
    }
    return BatchReplayCheckpoint.model_validate({**payload, "checkpoint_hash": _hash(payload)})


def write_batch_replay_checkpoint(checkpoint: BatchReplayCheckpoint, path: Path) -> None:
    """Durably replace only the mutable resume index after each finished case."""

    checkpoint = BatchReplayCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
    if path.is_symlink():
        raise BatchReplayInputError("batch checkpoint cannot be a symlink")
    content = canonical_json_bytes(checkpoint.model_dump(mode="json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_batch_replay_checkpoint(path: Path) -> BatchReplayCheckpoint:
    if path.is_symlink():
        raise BatchReplayInputError("batch checkpoint cannot be a symlink")
    return BatchReplayCheckpoint.model_validate_json(path.read_bytes())


def _saved_result_is_valid(results_dir: Path, entry: BatchReplayCheckpointEntry) -> bool:
    if entry.result_hash is None:
        return False
    path = results_dir / "cases" / f"{entry.result_hash}.json"
    try:
        if path.is_symlink():
            return False
        content = path.read_bytes()
        result = BatchReplayCaseResult.model_validate_json(content)
        return (
            content == canonical_json_bytes(result.model_dump(mode="json"))
            and result.result_hash == entry.result_hash
            and result.case_id == entry.case_id
        )
    except (OSError, ValueError):
        return False


def _load_ready_data(
    *,
    project_dir: Path,
    ready: ReplayReadyInput,
    artifacts: dict[str, ReplayMarketDataArtifact],
    supplements: dict[str, FundingMarkSupplementArtifact],
) -> tuple[pd.DataFrame, FundingDataset]:
    candle = artifacts.get(ready.candle_artifact_hash)
    if (
        candle is None
        or candle.role != "CANDLE_ONE_MINUTE"
        or candle.request_hash != ready.request_hash
    ):
        raise ReplayDataInputError("manifest Candle artifact is unavailable or mismatched")
    loaded_candles = load_replay_market_data_artifact(
        project_dir=project_dir, request=ready.request, artifact=candle
    )
    if not isinstance(loaded_candles, pd.DataFrame):
        raise ReplayDataInputError("manifest Candle artifact loaded as the wrong type")

    if ready.funding_artifact_hash is None:
        funding = FundingDataset(
            coverage_start=ready.request.start,
            coverage_end=ready.request.end_exclusive,
            expected_settlement_times=(),
            observations=(),
        )
    else:
        artifact = artifacts.get(ready.funding_artifact_hash)
        if (
            artifact is None
            or artifact.role != "FUNDING"
            or artifact.request_hash != ready.request_hash
        ):
            raise ReplayDataInputError("manifest Funding artifact is unavailable or mismatched")
        loaded_funding = load_replay_market_data_artifact(
            project_dir=project_dir, request=ready.request, artifact=artifact
        )
        if not isinstance(loaded_funding, FundingDataset):
            raise ReplayDataInputError("manifest Funding artifact loaded as the wrong type")
        funding = loaded_funding
        if ready.funding_mark_supplement_hashes:
            by_time = {item.settlement_time: item for item in funding.observations}
            resolved = dict(by_time)
            for supplement_hash in ready.funding_mark_supplement_hashes:
                supplement = supplements.get(supplement_hash)
                if (
                    supplement is None
                    or supplement.request_hash != ready.request_hash
                    or supplement.funding_artifact_hash != artifact.artifact_hash
                ):
                    raise ReplayDataInputError(
                        "manifest Funding mark supplement is unavailable or mismatched"
                    )
                mark = verify_funding_mark_supplement(project_dir=project_dir, artifact=supplement)
                observation = by_time.get(supplement.funding_time)
                if observation is None or observation.rate != supplement.funding_rate:
                    raise ReplayDataInputError(
                        "manifest Funding mark supplement event does not match Funding"
                    )
                resolved[supplement.funding_time] = observation.model_copy(
                    update={"mark_price": mark}
                )
            funding = funding.model_copy(
                update={
                    "observations": tuple(
                        resolved[item.settlement_time] for item in funding.observations
                    )
                }
            )
    observed_times = tuple(
        item.settlement_time
        for item in funding.observations
        if ready.request.start
        < item.settlement_time
        < ready.request.end_exclusive - pd.Timedelta(minutes=1)
    )
    if observed_times != ready.required_funding_times:
        raise ReplayDataInputError("Funding observations changed from the ready manifest")
    return loaded_candles, funding


def _result(
    *,
    manifest: ReplayReadyManifest,
    plan: SensitivityPlan,
    ready: ReplayReadyInput,
    spec: BatchReplayCaseSpec,
    candles: pd.DataFrame,
    funding: FundingDataset,
    costed: CostedReplayResult | None,
    scheduler_status: ReplayCaseStatus,
    blocked_by_logical_signal_id: str | None,
) -> BatchReplayCaseResult:
    summary: CanonicalReplaySummary | None = None
    gross_r: Decimal | None = None
    fees = Decimal(0)
    slippage = Decimal(0)
    funding_cash_flow = Decimal(0)
    net_r: Decimal | None = None
    mae_r: Decimal | None = None
    mfe_r: Decimal | None = None
    exit_reason: str | None = None
    if costed is not None:
        adjusted = apply_funding(costed, funding)
        if adjusted.funding_data_missing:
            raise ReplayDataInputError("P7 reported missing Funding for a replay-ready input")
        excursions = calculate_excursions(costed, candles)
        summary = build_canonical_summary(adjusted, excursions)
        gross_r = costed.gross_r
        fees = costed.total_fee
        slippage = costed.total_slippage_cost
        funding_cash_flow = adjusted.funding_cash_flow
        net_r = adjusted.net_r
        mae_r = excursions.mae_r if excursions is not None else None
        mfe_r = excursions.mfe_r if excursions is not None else None
        exit_reason = (
            costed.trade.exit_reason.value if costed.trade.exit_reason is not None else None
        )
    payload = {
        "schema_version": "p9-batch-replay-result/0.2.0",
        "runner_version": BATCH_REPLAY_VERSION,
        "replay_version": REPLAY_VERSION,
        "case_id": spec.case_id,
        "execution_hash": spec.execution_hash,
        "request_hash": ready.request_hash,
        "input_hash": ready.input_hash,
        "ready_manifest_hash": manifest.manifest_hash,
        "source_scan_report_hash": ready.source_scan_report_hash,
        "request_set_hash": ready.request_set_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "candidate_hash": spec.candidate_hash,
        "parameter_content_hash": spec.parameter_content_hash,
        "scenario": spec.scenario.value,
        "candle_artifact_hash": ready.candle_artifact_hash,
        "funding_artifact_hash": ready.funding_artifact_hash,
        "funding_schedule_hash": ready.funding_schedule_hash,
        "market_data_hash": ready.market_data_hash,
        "registry_content_hash": ready.registry_content_hash,
        "rule_content_hash": ready.rule_content_hash,
        "rule_verification_status": ready.rule_verification_status,
        "rule_confidence": ready.rule_confidence,
        "approximate_historical_rule_warnings": list(ready.approximate_historical_rule_warnings),
        "funding_mark_supplement_hashes": list(ready.funding_mark_supplement_hashes),
        "funding_mark_warnings": list(ready.funding_mark_warnings),
        "gross_r": _decimal_text(gross_r),
        "fees": _decimal_text(fees),
        "slippage": _decimal_text(slippage),
        "funding": _decimal_text(funding_cash_flow),
        "net_r": _decimal_text(net_r),
        "mae_r": _decimal_text(mae_r),
        "mfe_r": _decimal_text(mfe_r),
        "exit_reason": exit_reason,
        "scheduler_status": scheduler_status.value,
        "blocked_by_logical_signal_id": blocked_by_logical_signal_id,
        "p7_summary": summary.model_dump(mode="json") if summary is not None else None,
        "validation_locked_test_authorized": False,
    }
    return BatchReplayCaseResult.model_validate({**payload, "result_hash": _hash(payload)})


def _write_result(result: BatchReplayCaseResult, results_dir: Path) -> Path:
    root = results_dir.resolve()
    destination = root / "cases" / f"{result.result_hash}.json"
    if not destination.resolve().is_relative_to(root):
        raise BatchReplayInputError("batch result path escapes its output directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, canonical_json_bytes(result.model_dump(mode="json")))
    return destination


def run_replay_batch(
    *,
    project_dir: Path,
    manifest: ReplayReadyManifest,
    plan: SensitivityPlan,
    parameter_versions: tuple[DevParameterVersion, ...],
    market_data: tuple[ReplayMarketDataArtifact, ...],
    funding_mark_supplements: tuple[FundingMarkSupplementArtifact, ...] = (),
    results_dir: Path,
    checkpoint_path: Path,
    candidate_hashes: tuple[str, ...] | None = None,
    cost_scenarios: tuple[CostScenario, ...] | None = None,
) -> BatchReplayCheckpoint:
    """Execute pending cases deterministically and checkpoint after every outcome."""

    manifest = ReplayReadyManifest.model_validate(manifest.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    specs = plan_batch_replay_cases(
        manifest=manifest,
        plan=plan,
        parameter_versions=parameter_versions,
        candidate_hashes=candidate_hashes,
        cost_scenarios=cost_scenarios,
    )
    ready_by_hash = {item.request_hash: item for item in manifest.inputs}
    validated_artifacts = tuple(
        ReplayMarketDataArtifact.model_validate(item.model_dump(mode="json"))
        for item in market_data
    )
    artifacts = {item.artifact_hash: item for item in validated_artifacts}
    if len(artifacts) != len(validated_artifacts):
        raise BatchReplayInputError("market-data artifacts must not be duplicated")
    validated_supplements = tuple(
        FundingMarkSupplementArtifact.model_validate(item.model_dump(mode="json"))
        for item in funding_mark_supplements
    )
    supplements = {item.artifact_hash: item for item in validated_supplements}
    if len(supplements) != len(validated_supplements):
        raise BatchReplayInputError("Funding mark supplements must not be duplicated")

    previous = load_batch_replay_checkpoint(checkpoint_path) if checkpoint_path.exists() else None
    specs_by_id = {item.case_id: item for item in specs}
    entries: dict[str, BatchReplayCheckpointEntry] = {}
    if previous is not None:
        for entry in previous.entries:
            spec = specs_by_id.get(entry.case_id)
            if (
                spec is not None
                and entry.state == "COMPLETE"
                and entry.execution_hash == spec.execution_hash
                and _saved_result_is_valid(results_dir, entry)
            ):
                entries[entry.case_id] = entry

    checkpoint = _checkpoint(
        manifest=manifest,
        plan=plan,
        specs=specs,
        entries=tuple(entries.values()),
    )
    write_batch_replay_checkpoint(checkpoint, checkpoint_path)
    loaded: dict[str, tuple[pd.DataFrame, FundingDataset]] = {}
    groups: dict[tuple[str, CostScenario], list[BatchReplayCaseSpec]] = {}
    for spec in specs:
        groups.setdefault((spec.candidate_hash, spec.scenario), []).append(spec)
    for group_key in sorted(groups, key=lambda item: (item[0], item[1].value)):
        group = sorted(
            groups[group_key],
            key=lambda item: (
                ready_by_hash[item.request_hash].request.request.armed.confirmation_close,
                ready_by_hash[item.request_hash].request.request.armed.symbol,
                ready_by_hash[item.request_hash].request.request.armed.logical_signal_id,
            ),
        )
        pending = [item for item in group if item.case_id not in entries]
        if not pending:
            continue
        try:
            replay_inputs: list[ReplayCase] = []
            for spec in group:
                ready = ready_by_hash[spec.request_hash]
                data = loaded.get(ready.input_hash)
                if data is None:
                    data = _load_ready_data(
                        project_dir=project_dir,
                        ready=ready,
                        artifacts=artifacts,
                        supplements=supplements,
                    )
                    loaded[ready.input_hash] = data
                replay_inputs.append(
                    ReplayCase(
                        request=ready.request.request,
                        candles=data[0],
                        scenario=spec.scenario,
                    )
                )
            replayed = {
                item.logical_signal_id: item for item in replay_cases(tuple(replay_inputs)).cases
            }
        except Exception as error:
            for spec in pending:
                entries[spec.case_id] = BatchReplayCheckpointEntry(
                    case_id=spec.case_id,
                    execution_hash=spec.execution_hash,
                    request_hash=spec.request_hash,
                    candidate_hash=spec.candidate_hash,
                    scenario=spec.scenario,
                    state="FAILED",
                    failure_reason=f"{type(error).__name__}: {error}",
                )
                checkpoint = _checkpoint(
                    manifest=manifest,
                    plan=plan,
                    specs=specs,
                    entries=tuple(entries.values()),
                )
                write_batch_replay_checkpoint(checkpoint, checkpoint_path)
            continue

        for spec in pending:
            ready = ready_by_hash[spec.request_hash]
            data = loaded[ready.input_hash]
            try:
                replayed_case = replayed[ready.request.request.armed.logical_signal_id]
                result = _result(
                    manifest=manifest,
                    plan=plan,
                    ready=ready,
                    spec=spec,
                    candles=data[0],
                    funding=data[1],
                    costed=replayed_case.replay,
                    scheduler_status=replayed_case.status,
                    blocked_by_logical_signal_id=(replayed_case.blocked_by_logical_signal_id),
                )
                _write_result(result, results_dir)
                entry = BatchReplayCheckpointEntry(
                    case_id=spec.case_id,
                    execution_hash=spec.execution_hash,
                    request_hash=spec.request_hash,
                    candidate_hash=spec.candidate_hash,
                    scenario=spec.scenario,
                    state="COMPLETE",
                    result_hash=result.result_hash,
                )
            except Exception as error:
                entry = BatchReplayCheckpointEntry(
                    case_id=spec.case_id,
                    execution_hash=spec.execution_hash,
                    request_hash=spec.request_hash,
                    candidate_hash=spec.candidate_hash,
                    scenario=spec.scenario,
                    state="FAILED",
                    failure_reason=f"{type(error).__name__}: {error}",
                )
            entries[spec.case_id] = entry
            checkpoint = _checkpoint(
                manifest=manifest,
                plan=plan,
                specs=specs,
                entries=tuple(entries.values()),
            )
            write_batch_replay_checkpoint(checkpoint, checkpoint_path)
    return checkpoint
