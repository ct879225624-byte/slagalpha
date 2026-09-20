"""Ledger-bound P9 Phase 2 replan orchestration.

This module deliberately does not scan candles.  It takes the authenticated
confirmed-trigger ledger, reruns the existing P6 planners for one frozen
candidate, and emits an independent accepted-request set.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.dev_source_scan import (
    AcceptedP6Request,
    ConfirmedTriggerLedger,
)
from slagalpha.research.parameters import (
    DevParameterVersion,
    require_parameter_plan_binding,
)
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    ReplayDataInputError,
    build_dev_replay_data_request,
)
from slagalpha.research.scan_plan import model_hash
from slagalpha.research.scan_trade_plan import _approximate_tick_impact
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest
from slagalpha.strategy.plans import (
    EntryStopEvaluation,
    TakeProfitEvaluation,
    build_entry_stop,
    build_take_profit,
)

FINAL_LEDGER_HASH = "aa4d993bd3a8a039438c57d87f32e8d72c25b48d32c8a8a87caa5d8bc6f44f59"
EXPECTED_RECORD_COUNT = 2946
EXECUTOR_VERSION = "p9-replan-executor/0.1.0"
BASELINE_CANDIDATE_HASH = "e7af960eec91b8e8b8abf5484f3ef98aab73a90660121714746c3b24408d49c5"
FROZEN_REPLAN_CANDIDATES = {
    "bd7578e397a1a0d72c52a4d0244fa9dd2d73792f07b8181d886fefe695060d38":
        "4fa144e32ee64ca7d724edc3508e484e001b2e9de3009b7648ac38bfc28b3327",
    "2411b911264e674876c3bc19f22b6937c2d71033e7eb1ab2769c69c5daadc77a":
        "92981dbecd337f6defda80f953cff578089725ca51c74eb4a4118fdd0f3b58d5",
    "0b515c4c262e0c2f23551a6db12e7fe56b7ca992e4c939e57deb6c690a474950":
        "edbfb8a5bde6bc46c898dd641b679c429d4322fbd5722a202f0266845a80360f",
    "3ba25f08b6ba9c05b96a2f6d54ca8b73aeda685b823f34aafd4e912f526e7631":
        "96f715fe74f3ea081cf43f538cb5f651e94114ad484686ea6d9267cbd2b9f37d",
}
SUPPORTED_CANDIDATES = {
    BASELINE_CANDIDATE_HASH: "81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b",
    **FROZEN_REPLAN_CANDIDATES,
}


class ReplanExecutorError(ValueError):
    """Fail-closed input or orchestration error."""


class ReplanCandidateFailed(ReplanExecutorError):
    """An unexpected P6 exception stopped one candidate without skipping rows."""

    def __init__(self, candidate_hash: str, record_index: int, error: Exception) -> None:
        super().__init__(
            f"candidate {candidate_hash} failed at ledger record {record_index}: "
            f"{type(error).__name__}: {error}"
        )
        self.candidate_hash = candidate_hash
        self.record_index = record_index
        self.original = error


class ReplanBaselineComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    common_accepted: tuple[str, ...]
    newly_accepted: tuple[str, ...]
    removed_accepted: tuple[str, ...]


class ReplanCandidateArtifact(BaseModel):
    """One immutable candidate-bound P6 replan result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-replan-candidate/0.1.0"] = "p9-replan-candidate/0.1.0"
    status: Literal["COMPLETE", "FAILED"]
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_final_ledger_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_record_count: int = Field(ge=0, strict=True)
    processed_record_count: int = Field(ge=0, strict=True)
    p6_status_counts: dict[str, int]
    entry_stop_rejected_count: int = Field(ge=0, strict=True)
    take_profit_rejected_count: int = Field(ge=0, strict=True)
    accepted_count: int = Field(ge=0, strict=True)
    request_boundary_rejected_count: int = Field(ge=0, strict=True)
    accepted_logical_signal_ids: tuple[str, ...]
    accepted_request_payloads: tuple[dict[str, Any], ...]
    accepted_request_set_hash: str | None
    accepted_symbol_count: int = Field(ge=0, strict=True)
    baseline_comparison: ReplanBaselineComparison
    provenance: dict[str, Any]
    failure: str | None = None
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.input_record_count != EXPECTED_RECORD_COUNT:
            raise ValueError("replan artifact input record count must be 2946")
        if self.processed_record_count > self.input_record_count:
            raise ValueError("processed records exceed input records")
        if self.status == "COMPLETE" and self.processed_record_count != self.input_record_count:
            raise ValueError("complete replan must process every ledger record")
        expected = {
            "REJECTED_ENTRY_STOP": self.entry_stop_rejected_count,
            "REJECTED_TAKE_PROFIT": self.take_profit_rejected_count,
            "REJECTED_REQUEST_BOUNDARY": self.request_boundary_rejected_count,
            "ACCEPTED_PLAN": self.accepted_count,
        }
        if self.p6_status_counts != expected:
            raise ValueError("P6 status counts do not reconcile")
        if self.status == "COMPLETE":
            if sum(expected.values()) != self.input_record_count:
                raise ValueError("complete P6 counts do not reconcile with ledger")
            if self.accepted_count != len(self.accepted_logical_signal_ids):
                raise ValueError("accepted signal IDs do not reconcile")
            if self.accepted_count != len(self.accepted_request_payloads):
                raise ValueError("accepted requests do not reconcile")
            if self.accepted_request_set_hash is None:
                raise ValueError("complete replan is missing request-set hash")
        payload = self.model_dump(mode="json", exclude={"artifact_hash"})
        if self.artifact_hash != _hash(payload):
            raise ValueError("replan artifact hash mismatch")
        return self


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _visibility_candles(confirmation_close: datetime, minutes: int, count: int) -> pd.DataFrame:
    """Provide the timestamp-only P6 visibility contract.

    P6's target selector reads only zone evidence; candle values are not used
    after the existing lookback/closed/ordering checks.  The ledger already
    stores the point-in-time zones, so this avoids a second scanner or a market
    data read while preserving the planner's visibility boundary.
    """

    interval = timedelta(minutes=minutes)
    last_open = confirmation_close - interval
    opens = [last_open - interval * (count - 1 - index) for index in range(count)]
    return pd.DataFrame(
        {
            "open_time": pd.DatetimeIndex(opens),
            "close_time_exclusive": pd.DatetimeIndex([item + interval for item in opens]),
            "is_closed": [True] * count,
        }
    )


def load_final_ledger(
    path: Path, *, expected_hash: str = FINAL_LEDGER_HASH
) -> ConfirmedTriggerLedger:
    if path.stem != expected_hash:
        raise ReplanExecutorError("ledger filename does not match required final ledger hash")
    try:
        ledger = ConfirmedTriggerLedger.model_validate_json(path.read_bytes())
    except Exception as error:
        raise ReplanExecutorError(f"invalid final confirmed-trigger ledger: {error}") from error
    if ledger.ledger_hash != expected_hash:
        raise ReplanExecutorError("final ledger hash mismatch")
    if ledger.record_count != EXPECTED_RECORD_COUNT:
        raise ReplanExecutorError("final ledger record count must be 2946")
    ids = [item.logical_signal_id for item in ledger.records]
    if len(ids) != len(set(ids)):
        raise ReplanExecutorError("duplicate logical_signal_id in final ledger")
    return ledger


def _load_parameter(
    parameter_path: Path,
    *,
    plan: SensitivityPlan,
    candidate_hash: str,
    expected_content_hash: str | None,
) -> DevParameterVersion:
    if parameter_path.stem != (expected_content_hash or parameter_path.stem):
        raise ReplanExecutorError("parameter filename does not match its content hash")
    try:
        parameter = DevParameterVersion.model_validate_json(parameter_path.read_bytes())
        require_parameter_plan_binding(parameter, plan)
    except Exception as error:
        raise ReplanExecutorError(f"invalid candidate parameter binding: {error}") from error
    if parameter.candidate.candidate_hash != candidate_hash:
        raise ReplanExecutorError("candidate hash does not match parameter candidate")
    if expected_content_hash is not None and parameter.content_hash != expected_content_hash:
        raise ReplanExecutorError("parameter content hash mismatch")
    return parameter


def _load_universes(data_dir: Path) -> dict[str, UniverseSnapshot]:
    result: dict[str, UniverseSnapshot] = {}
    for path in sorted((data_dir / "manifests" / "universe_snapshot").glob("*.json")):
        try:
            snapshot = UniverseSnapshot.model_validate_json(path.read_bytes())
        except Exception as error:
            raise ReplanExecutorError(f"invalid Universe snapshot: {path}") from error
        digest = model_hash(snapshot)
        if digest in result and result[digest] != snapshot:
            raise ReplanExecutorError("duplicate Universe content hash")
        result[digest] = snapshot
    return result


def _load_registry(data_dir: Path, expected_hash: str) -> ContractRegistry:
    paths = sorted((data_dir / "manifests" / "contract_registry").glob("*.json"))
    matches: list[ContractRegistry] = []
    for path in paths:
        try:
            registry = ContractRegistry.model_validate_json(path.read_bytes())
        except Exception as error:
            raise ReplanExecutorError(f"invalid contract registry: {path}") from error
        if model_hash(registry) == expected_hash:
            matches.append(registry)
    if len(matches) != 1:
        raise ReplanExecutorError("final ledger contract registry context is missing or ambiguous")
    return matches[0]


def _load_split(data_dir: Path, plan: SensitivityPlan) -> ResearchSplitManifest:
    path = data_dir / "manifests" / "research_split" / f"{plan.split_hash}.json"
    if not path.exists():
        raise ReplanExecutorError("sensitivity split manifest is missing")
    try:
        return ResearchSplitManifest.model_validate_json(path.read_bytes())
    except Exception as error:
        raise ReplanExecutorError("invalid sensitivity split manifest") from error


def _baseline_ids(ledger: ConfirmedTriggerLedger) -> set[str]:
    return {
        item.logical_signal_id
        for item in ledger.records
        if item.baseline_p6_status == "ACCEPTED_PLAN"
    }


def _status_payload(
    *,
    candidate_hash: str,
    parameter: DevParameterVersion,
    ledger: ConfirmedTriggerLedger,
    processed: int,
    counts: dict[str, int],
    accepted: list[AcceptedP6Request],
    baseline_ids: set[str],
    status: Literal["COMPLETE", "FAILED"],
    failure: str | None = None,
) -> dict[str, Any]:
    accepted_ids = tuple(item.request.request.armed.logical_signal_id for item in accepted)
    accepted_payloads = tuple(item.model_dump(mode="json") for item in accepted)
    accepted_set = set(accepted_ids)
    comparison = ReplanBaselineComparison(
        common_accepted=tuple(sorted(accepted_set & baseline_ids)),
        newly_accepted=tuple(sorted(accepted_set - baseline_ids)),
        removed_accepted=tuple(sorted(baseline_ids - accepted_set)),
    )
    request_set_hash = (
        _hash({"accepted_requests": list(accepted_payloads)})
        if status == "COMPLETE"
        else None
    )
    payload = {
        "schema_version": "p9-replan-candidate/0.1.0",
        "status": status,
        "candidate_hash": candidate_hash,
        "parameter_content_hash": parameter.content_hash,
        "source_final_ledger_hash": ledger.ledger_hash,
        "input_record_count": ledger.record_count,
        "processed_record_count": processed,
        "p6_status_counts": dict(counts),
        "entry_stop_rejected_count": counts["REJECTED_ENTRY_STOP"],
        "take_profit_rejected_count": counts["REJECTED_TAKE_PROFIT"],
        "accepted_count": counts["ACCEPTED_PLAN"],
        "request_boundary_rejected_count": counts["REJECTED_REQUEST_BOUNDARY"],
        "accepted_logical_signal_ids": accepted_ids,
        "accepted_request_payloads": accepted_payloads,
        "accepted_request_set_hash": request_set_hash,
        "accepted_symbol_count": len({item.request.request.armed.symbol for item in accepted}),
        "baseline_comparison": comparison.model_dump(mode="json"),
        "provenance": {
            "executor_version": EXECUTOR_VERSION,
            "p6_entry_stop": "slagalpha.strategy.plans.build_entry_stop",
            "p6_take_profit": "slagalpha.strategy.plans.build_take_profit",
            "request_builder": "slagalpha.research.replay_inputs.build_dev_replay_data_request",
            "sensitivity_plan_hash": parameter.sensitivity_plan_hash,
            "scanner_executed": False,
            "replay_executed": False,
            "network_accessed": False,
            "validation_locked_test_authorized": False,
        },
        "failure": failure,
    }
    return {**payload, "artifact_hash": _hash(payload)}


def execute_replan_candidate(
    *,
    candidate_hash: str,
    ledger: ConfirmedTriggerLedger,
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    split: ResearchSplitManifest,
    registry: ContractRegistry,
    universes: dict[str, UniverseSnapshot],
) -> ReplanCandidateArtifact:
    """Run one frozen candidate across every final-ledger trigger."""

    try:
        ledger = ConfirmedTriggerLedger.model_validate(ledger.model_dump(mode="json"))
    except Exception as error:
        raise ReplanExecutorError(f"final ledger validation failed: {error}") from error
    if ledger.ledger_hash != FINAL_LEDGER_HASH or ledger.record_count != EXPECTED_RECORD_COUNT:
        raise ReplanExecutorError("executor requires the authenticated final 2946-record ledger")
    if candidate_hash not in SUPPORTED_CANDIDATES:
        raise ReplanExecutorError("candidate is not one of the frozen Phase 2 candidates")
    if parameter.candidate.candidate_hash != candidate_hash:
        raise ReplanExecutorError("candidate hash does not match parameter candidate")
    try:
        require_parameter_plan_binding(parameter, plan)
    except Exception as error:
        raise ReplanExecutorError(f"candidate parameter binding failed: {error}") from error
    if split.split_hash != plan.split_hash or model_hash(registry) != ledger.registry_content_hash:
        raise ReplanExecutorError("candidate context does not match frozen plan/ledger")
    if len({item.logical_signal_id for item in ledger.records}) != ledger.record_count:
        raise ReplanExecutorError("duplicate logical_signal_id in ledger")

    counts = {
        "REJECTED_ENTRY_STOP": 0,
        "REJECTED_TAKE_PROFIT": 0,
        "REJECTED_REQUEST_BOUNDARY": 0,
        "ACCEPTED_PLAN": 0,
    }
    accepted: list[AcceptedP6Request] = []
    baseline_ids = _baseline_ids(ledger)
    params = parameter.candidate.parameters

    for index, record in enumerate(ledger.records):
        try:
            # Empty hourly pivot/zone tuples are valid point-in-time states;
            # missing fields are rejected by the authenticated ledger model.
            if record.visible_hourly_pivots is None or record.visible_hourly_zones is None:
                raise ReplanExecutorError("hourly point-in-time context is missing")
            universe = universes.get(record.universe_content_hash)
            if universe is None:
                raise ReplanExecutorError(
                    f"Universe context missing for {record.logical_signal_id}"
                )
            entry_stop: EntryStopEvaluation = build_entry_stop(
                record.entry_stop_request,
                entry_ttl_bars=params.entry_ttl_bars,
                stop_atr_multiplier=params.stop_atr_multiplier,
            )
            if not entry_stop.accepted:
                counts["REJECTED_ENTRY_STOP"] += 1
                continue
            take_profit: TakeProfitEvaluation = build_take_profit(
                entry_stop,
                fifteen_minute_candles=_visibility_candles(
                    record.confirmation_close_time, 15, 96
                ),
                one_hour_candles=_visibility_candles(
                    record.confirmation_close_time, 60, 60
                ),
                fifteen_minute_zones=record.visible_zones,
                one_hour_zones=record.visible_hourly_zones,
            )
            if not take_profit.accepted:
                counts["REJECTED_TAKE_PROFIT"] += 1
                continue
            try:
                request: DevReplayDataRequest = build_dev_replay_data_request(
                    trade_plan=take_profit,
                    plan=plan,
                    parameter=parameter,
                    split=split,
                    universe=universe,
                    registry=registry,
                )
            except ReplayDataInputError:
                counts["REJECTED_REQUEST_BOUNDARY"] += 1
                continue
            rules = tuple(
                item for item in registry.entries
                if item.symbol == record.symbol
                and item.effective_from <= request.start
                and (item.effective_to is None or request.end_exclusive <= item.effective_to)
            )
            if len(rules) != 1:
                raise ReplanExecutorError("accepted request lacks one covering contract rule")
            impact = _approximate_tick_impact(rules[0], entry_stop, take_profit)
            accepted.append(AcceptedP6Request(request=request, approximate_tick_impact=impact))
            counts["ACCEPTED_PLAN"] += 1
        except ReplayDataInputError:
            counts["REJECTED_REQUEST_BOUNDARY"] += 1
        except ReplanExecutorError:
            # Authenticated-context and orchestration violations fail closed;
            # only unexpected P6 exceptions become a candidate FAILED artifact.
            raise
        except Exception as error:
            raise ReplanCandidateFailed(candidate_hash, index, error) from error

    payload = _status_payload(
        candidate_hash=candidate_hash,
        parameter=parameter,
        ledger=ledger,
        processed=ledger.record_count,
        counts=counts,
        accepted=accepted,
        baseline_ids=baseline_ids,
        status="COMPLETE",
    )
    return ReplanCandidateArtifact.model_validate(payload)


def build_failed_artifact(
    *, ledger: ConfirmedTriggerLedger, parameter: DevParameterVersion, error: ReplanCandidateFailed,
) -> ReplanCandidateArtifact:
    counts = {
        "REJECTED_ENTRY_STOP": 0,
        "REJECTED_TAKE_PROFIT": 0,
        "REJECTED_REQUEST_BOUNDARY": 0,
        "ACCEPTED_PLAN": 0,
    }
    payload = _status_payload(
        candidate_hash=parameter.candidate.candidate_hash,
        parameter=parameter,
        ledger=ledger,
        processed=error.record_index,
        counts=counts,
        accepted=[],
        baseline_ids=_baseline_ids(ledger),
        status="FAILED",
        failure=str(error),
    )
    return ReplanCandidateArtifact.model_validate(payload)


def write_replan_artifact(artifact: ReplanCandidateArtifact, output_dir: Path) -> Path:
    artifact = ReplanCandidateArtifact.model_validate(artifact.model_dump(mode="json"))
    destination = output_dir / f"{artifact.artifact_hash}.replan.json"
    content = canonical_json_bytes(artifact.model_dump(mode="json"))
    if destination.exists() and destination.read_bytes() != content:
        raise ReplanExecutorError(f"existing replan artifact changed: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
