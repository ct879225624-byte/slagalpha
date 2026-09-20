"""Offline fail-closed preparation for P9 DEV replay inputs and resume state."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.backtest.analytics import FundingDataset
from slagalpha.backtest.costs import CostScenario
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.funding_mark_supplement import (
    FundingMarkSupplementArtifact,
    verify_funding_mark_supplement,
)
from slagalpha.research.replay_inputs import DevReplayDataRequest, ReplayDataInputError, Sha256
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    load_replay_market_data_artifact,
)

if TYPE_CHECKING:
    from slagalpha.data.binance_usdm_transport import BinanceTransportPlan
    from slagalpha.research.market_data_requirements import MarketDataRequirementPlan
else:
    BinanceTransportPlan = Any
    MarketDataRequirementPlan = Any

IssueCategory = Literal[
    "CANDLE_MISSING",
    "FUNDING_MISSING",
    "HASH_PROVENANCE",
    "DUPLICATE_DISORDER_BOUNDARY",
    "SYMBOL_UTC",
    "SCAN_INCOMPLETE",
]


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class FundingScheduleArtifact(BaseModel):
    """Versioned external settlement schedule; rates remain in Funding artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["funding-schedule/0.1.0"] = "funding-schedule/0.1.0"
    symbol: str
    coverage_start: datetime
    coverage_end_exclusive: datetime
    settlement_times: tuple[datetime, ...]
    provider: str
    schedule_version: str
    source_ref: str
    source_content_sha256: Sha256
    observed_at: datetime
    schedule_hash: Sha256

    @field_validator("coverage_start", "coverage_end_exclusive", "observed_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Funding schedule timestamps must use UTC")
        return value

    @field_validator("settlement_times", mode="after")
    @classmethod
    def validate_settlement_times(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if any(value.tzinfo is None or value.utcoffset() != timedelta(0) for value in values):
            raise ValueError("Funding settlement times must use UTC")
        if values != tuple(sorted(set(values))):
            raise ValueError("Funding settlement times must be unique and ordered")
        return values

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        if (
            not self.symbol.strip()
            or self.symbol != self.symbol.strip().upper()
            or self.coverage_end_exclusive <= self.coverage_start
            or not all(
                (
                    self.provider.strip(),
                    self.schedule_version.strip(),
                    self.source_ref.strip(),
                )
            )
        ):
            raise ValueError("Funding schedule provenance and coverage are required")
        if self.observed_at < self.coverage_end_exclusive:
            raise ValueError("Funding schedule must be observed after its covered history")
        if any(
            value < self.coverage_start or value >= self.coverage_end_exclusive
            for value in self.settlement_times
        ):
            raise ValueError("Funding settlement time is outside schedule coverage")
        payload = self.model_dump(mode="json", exclude={"schedule_hash"})
        if self.schedule_hash != _hash(payload):
            raise ValueError("Funding schedule content hash mismatch")
        return self


def build_funding_schedule_artifact(
    *,
    symbol: str,
    coverage_start: datetime,
    coverage_end_exclusive: datetime,
    settlement_times: tuple[datetime, ...],
    provider: str,
    schedule_version: str,
    source_ref: str,
    source_content_sha256: str,
    observed_at: datetime,
) -> FundingScheduleArtifact:
    payload = {
        "schema_version": "funding-schedule/0.1.0",
        "symbol": symbol,
        "coverage_start": coverage_start.isoformat().replace("+00:00", "Z"),
        "coverage_end_exclusive": coverage_end_exclusive.isoformat().replace("+00:00", "Z"),
        "settlement_times": [
            value.isoformat().replace("+00:00", "Z") for value in settlement_times
        ],
        "provider": provider,
        "schedule_version": schedule_version,
        "source_ref": source_ref,
        "source_content_sha256": source_content_sha256,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }
    return FundingScheduleArtifact.model_validate({**payload, "schedule_hash": _hash(payload)})


class ReplayPreflightIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_hash: Sha256
    symbol: str
    category: IssueCategory
    code: str
    message: str


class ReplayReadyInput(BaseModel):
    """Existing P7 request plus verified, content-addressed data references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-dev-replay-input/0.1.0"] = "p9-dev-replay-input/0.1.0"
    dataset_role: Literal["DEV"] = "DEV"
    source_scan_report_hash: Sha256
    request_set_hash: Sha256
    request: DevReplayDataRequest
    request_hash: Sha256
    candle_artifact_hash: Sha256
    funding_required: bool
    funding_artifact_hash: Sha256 | None
    funding_schedule_hash: Sha256
    required_funding_times: tuple[datetime, ...]
    market_data_hash: Sha256
    registry_content_hash: Sha256
    rule_content_hash: Sha256
    rule_verification_status: str
    rule_confidence: str
    approximate_historical_rule_warnings: tuple[str, ...]
    cost_scenarios: tuple[Literal["ZERO", "BASELINE", "STRESS"], ...]
    validation_locked_test_authorized: Literal[False] = False
    input_hash: Sha256
    funding_mark_supplement_hashes: tuple[Sha256, ...] = ()
    funding_mark_warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if self.request_hash != self.request.request_hash:
            raise ValueError("replay input request hash mismatch")
        if self.registry_content_hash != self.request.registry_content_hash:
            raise ValueError("replay input registry provenance mismatch")
        if self.rule_content_hash != self.request.rule_content_hash:
            raise ValueError("replay input rule provenance mismatch")
        if self.approximate_historical_rule_warnings != self.request.rule_warning_codes:
            raise ValueError("replay input historical-rule warnings changed")
        if self.cost_scenarios != tuple(item.value for item in CostScenario):
            raise ValueError("replay input must retain the frozen cost scenarios")
        if self.funding_required != bool(self.required_funding_times):
            raise ValueError("Funding requirement does not match the external schedule")
        if self.funding_required != (self.funding_artifact_hash is not None):
            raise ValueError("required Funding must have one verified artifact")
        market_payload: dict[str, Any] = {
            "candle_artifact_hash": self.candle_artifact_hash,
            "funding_artifact_hash": self.funding_artifact_hash,
            "funding_schedule_hash": self.funding_schedule_hash,
        }
        if self.funding_mark_supplement_hashes:
            market_payload["funding_mark_supplement_hashes"] = list(
                self.funding_mark_supplement_hashes
            )
        if self.market_data_hash != _hash(market_payload):
            raise ValueError("replay market-data hash mismatch")
        payload = self.model_dump(mode="json", exclude={"input_hash"})
        if not self.funding_mark_supplement_hashes and not self.funding_mark_warnings:
            payload.pop("funding_mark_supplement_hashes", None)
            payload.pop("funding_mark_warnings", None)
        if self.input_hash != _hash(payload):
            raise ValueError("replay input content hash mismatch")
        return self


class ReplayReadyManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-dev-replay-ready-manifest/0.1.0"] = (
        "p9-dev-replay-ready-manifest/0.1.0"
    )
    dataset_role: Literal["DEV"] = "DEV"
    source_scan_report_hash: Sha256
    request_set_hash: Sha256
    accepted_request_count: int = Field(ge=0, strict=True)
    ready_request_count: int = Field(ge=0, strict=True)
    inputs: tuple[ReplayReadyInput, ...]
    validation_locked_test_authorized: Literal[False] = False
    manifest_hash: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if (
            self.ready_request_count != len(self.inputs)
            or self.ready_request_count > self.accepted_request_count
        ):
            raise ValueError("ready request count does not reconcile")
        if any(
            item.source_scan_report_hash != self.source_scan_report_hash
            or item.request_set_hash != self.request_set_hash
            for item in self.inputs
        ):
            raise ValueError("replay-ready input binding differs from the manifest")
        hashes = tuple(item.request_hash for item in self.inputs)
        if hashes != tuple(sorted(set(hashes))):
            raise ValueError("replay-ready inputs must be unique and canonical")
        payload = self.model_dump(mode="json", exclude={"manifest_hash"})
        if self.manifest_hash != _hash(payload):
            raise ValueError("replay-ready manifest content hash mismatch")
        return self


class ReplayReadinessSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-dev-replay-readiness/0.1.0"] = "p9-dev-replay-readiness/0.1.0"
    source_scan_report_hash: Sha256
    accepted_request_count: int = Field(ge=0, strict=True)
    replay_ready_count: int = Field(ge=0, strict=True)
    not_ready_request_count: int = Field(default=0, ge=0, strict=True)
    candle_missing_blocked_count: int = Field(ge=0, strict=True)
    funding_missing_blocked_count: int = Field(ge=0, strict=True)
    funding_mark_price_missing_request_count: int = Field(default=0, ge=0, strict=True)
    funding_mark_price_missing_event_count: int = Field(default=0, ge=0, strict=True)
    hash_provenance_blocked_count: int = Field(ge=0, strict=True)
    duplicate_disorder_boundary_blocked_count: int = Field(ge=0, strict=True)
    symbol_utc_blocked_count: int = Field(ge=0, strict=True)
    symbol_count: int = Field(ge=0, strict=True)
    required_coverage_start: datetime | None
    required_coverage_end_exclusive: datetime | None
    ready_data_coverage_start: datetime | None
    ready_data_coverage_end_exclusive: datetime | None
    can_start_dev_replay: bool
    primary_missing: str
    issues: tuple[ReplayPreflightIssue, ...]
    requirements_plan_hash: Sha256 | None = None
    transport_plan_hash: Sha256 | None = None
    summary_hash: Sha256

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if (
            self.replay_ready_count > self.accepted_request_count
            or self.not_ready_request_count != self.accepted_request_count - self.replay_ready_count
        ):
            raise ValueError("ready count exceeds accepted request count")
        if self.can_start_dev_replay != (
            self.accepted_request_count > 0
            and self.replay_ready_count == self.accepted_request_count
            and not self.issues
        ):
            raise ValueError("DEV replay readiness does not reconcile")
        payload = self.model_dump(mode="json", exclude={"summary_hash"})
        if self.summary_hash != _hash(payload):
            raise ValueError("readiness summary content hash mismatch")
        return self


class ReplayPrepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: ReplayReadinessSummary
    manifest: ReplayReadyManifest


def _issue(
    request: DevReplayDataRequest, category: IssueCategory, code: str, message: str
) -> ReplayPreflightIssue:
    return ReplayPreflightIssue(
        request_hash=request.request_hash,
        symbol=request.request.armed.symbol,
        category=category,
        code=code,
        message=message,
    )


def _data_error_issue(
    request: DevReplayDataRequest, role: str, error: Exception
) -> ReplayPreflightIssue:
    message = str(error)
    lowered = message.lower()
    if any(
        word in lowered
        for word in (
            "hash",
            "provenance",
            "source",
            "symlink",
            "file missing",
            "outside project",
        )
    ):
        category: IssueCategory = "HASH_PROVENANCE"
        code = f"{role}_HASH_OR_PROVENANCE"
    elif role == "CANDLE" and "every requested minute" in lowered:
        category = "CANDLE_MISSING"
        code = "CANDLE_MINUTES_MISSING"
    elif role == "FUNDING" and any(
        word in lowered for word in ("mark_price", "markprice", "settlement value")
    ):
        category = "FUNDING_MISSING"
        code = "FUNDING_MARK_PRICE_MISSING"
    elif role == "FUNDING" and any(word in lowered for word in ("empty", "missing")):
        category = "FUNDING_MISSING"
        code = "FUNDING_RECORDS_MISSING"
    elif any(
        word in lowered
        for word in (
            "duplicate",
            "duplicated",
            "repeated",
            "out of order",
            "grid",
            "boundary",
            "partition",
            "interval",
            "aligned",
        )
    ):
        category = "DUPLICATE_DISORDER_BOUNDARY"
        code = f"{role}_DUPLICATE_DISORDER_OR_BOUNDARY"
    elif any(word in lowered for word in ("symbol", "utc", "timezone")):
        category = "SYMBOL_UTC"
        code = f"{role}_SYMBOL_OR_UTC"
    else:
        category = "DUPLICATE_DISORDER_BOUNDARY"
        code = f"{role}_DUPLICATE_DISORDER_OR_BOUNDARY"
    return _issue(request, category, code, message)


def _model_payload(value: Any) -> Any:
    """Accept the frozen models used by callers without widening the public contract."""

    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _validate_authenticated_bindings(
    *,
    report: DevSourceScanReport,
    requirements: Any | None,
    transport_plan: Any | None,
    funding_schedules: tuple[FundingScheduleArtifact, ...],
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Derive and, when supplied, authenticate post-scan and transport bindings.

    The derivation is offline and deterministic.  Optional arguments preserve the
    small direct Python API used by synthetic tests; the CLI supplies both saved
    artifacts for a real run.
    """

    messages: list[str] = []
    expected_requirements: Any | None = None
    bound_requirements: Any | None = None
    try:
        from slagalpha.research.market_data_requirements import (
            MarketDataRequirementPlan,
            build_market_data_requirement_plan,
        )

        expected_requirements = build_market_data_requirement_plan(report)
        if requirements is None:
            bound_requirements = expected_requirements
        else:
            bound_requirements = MarketDataRequirementPlan.model_validate(
                _model_payload(requirements)
            )
            if bound_requirements != expected_requirements:
                messages.append(
                    "requirements plan is not the authenticated plan derived from the source report"
                )
    except (ImportError, OSError, ValueError) as error:
        messages.append(f"requirements binding could not be verified: {error}")

    expected_transport: Any | None = None
    bound_transport: Any | None = None
    if bound_requirements is not None and report.status == "COMPLETE":
        try:
            from slagalpha.data.binance_usdm_transport import (
                BinanceTransportPlan,
                build_binance_transport_plan,
            )

            if transport_plan is not None:
                bound_transport = BinanceTransportPlan.model_validate(
                    _model_payload(transport_plan)
                )
                max_attempts = bound_transport.tasks[0].max_attempts
            else:
                max_attempts = 3
            expected_transport = build_binance_transport_plan(
                report=report,
                requirements=bound_requirements,
                max_attempts=max_attempts,
            )
            if transport_plan is None:
                bound_transport = expected_transport
            else:
                if bound_transport != expected_transport:
                    messages.append(
                        "transport plan is not the authenticated plan derived from the requirements"
                    )
        except (ImportError, OSError, ValueError) as error:
            messages.append(f"transport-plan binding could not be verified: {error}")
    elif transport_plan is not None:
        messages.append(
            "transport plan cannot be authenticated without a complete requirements plan"
        )

    requirements_hash = bound_requirements.plan_hash if bound_requirements is not None else None
    transport_hash = bound_transport.transport_plan_hash if bound_transport is not None else None

    # Versioned schedules emitted by the transport carry its hash and exact merged
    # requirement window in source_ref.  Validate those links when present; synthetic
    # fixtures may use a local URI and intentionally have no transport provenance.
    requirement_windows = {
        (item.symbol, window.start, window.end_exclusive)
        for item in (bound_requirements.requirements if bound_requirements is not None else ())
        for window in item.windows
    }
    schedule_keys = [
        (item.symbol, item.coverage_start, item.coverage_end_exclusive)
        for item in funding_schedules
    ]
    if len(schedule_keys) != len(set(schedule_keys)):
        messages.append("Funding schedules contain duplicate coverage windows")
    if bound_requirements is not None:
        missing_windows = requirement_windows - set(schedule_keys)
        if missing_windows:
            messages.append("Funding schedules do not cover every authenticated requirement window")
    for schedule in funding_schedules:
        if not schedule.source_ref.startswith("transport:"):
            continue
        parts = schedule.source_ref.split(":")
        expected_ref = (
            len(parts) == 5
            and transport_hash is not None
            and parts[1] == transport_hash
            and parts[2] == schedule.symbol
            and parts[3] == str(int(schedule.coverage_start.timestamp() * 1000))
            and parts[4] == str(int(schedule.coverage_end_exclusive.timestamp() * 1000) - 1)
        )
        if not expected_ref:
            messages.append(
                "Funding schedule provenance does not match the authenticated "
                f"transport plan: {schedule.schedule_hash}"
            )

    return requirements_hash, transport_hash, tuple(dict.fromkeys(messages))


def _build_ready_input(
    *,
    report: DevSourceScanReport,
    request: DevReplayDataRequest,
    candle: ReplayMarketDataArtifact,
    funding: ReplayMarketDataArtifact | None,
    schedule: FundingScheduleArtifact,
    required_times: tuple[datetime, ...],
    funding_mark_supplement_hashes: tuple[str, ...] = (),
    funding_mark_warnings: tuple[str, ...] = (),
) -> ReplayReadyInput:
    market_data: dict[str, Any] = {
        "candle_artifact_hash": candle.artifact_hash,
        "funding_artifact_hash": funding.artifact_hash if funding else None,
        "funding_schedule_hash": schedule.schedule_hash,
    }
    if funding_mark_supplement_hashes:
        market_data["funding_mark_supplement_hashes"] = list(funding_mark_supplement_hashes)
    payload = {
        "schema_version": "p9-dev-replay-input/0.1.0",
        "dataset_role": "DEV",
        "source_scan_report_hash": report.report_hash,
        "request_set_hash": report.request_set_hash,
        "request": request.model_dump(mode="json"),
        "request_hash": request.request_hash,
        **market_data,
        "funding_required": bool(required_times),
        "required_funding_times": [
            item.isoformat().replace("+00:00", "Z") for item in required_times
        ],
        "market_data_hash": _hash(market_data),
        "registry_content_hash": request.registry_content_hash,
        "rule_content_hash": request.rule_content_hash,
        "rule_verification_status": request.rule_verification_status.value,
        "rule_confidence": request.rule_confidence.value,
        "approximate_historical_rule_warnings": list(request.rule_warning_codes),
        "cost_scenarios": [item.value for item in CostScenario],
        "validation_locked_test_authorized": False,
    }
    if funding_mark_supplement_hashes:
        payload["funding_mark_supplement_hashes"] = list(funding_mark_supplement_hashes)
    if funding_mark_warnings:
        payload["funding_mark_warnings"] = list(funding_mark_warnings)
    return ReplayReadyInput.model_validate({**payload, "input_hash": _hash(payload)})


def prepare_dev_replay(
    *,
    project_dir: Path,
    report: DevSourceScanReport,
    market_data: tuple[ReplayMarketDataArtifact, ...],
    funding_schedules: tuple[FundingScheduleArtifact, ...],
    funding_mark_supplements: tuple[FundingMarkSupplementArtifact, ...] = (),
    requirements: MarketDataRequirementPlan | None = None,
    transport_plan: BinanceTransportPlan | None = None,
) -> ReplayPrepResult:
    """Validate every accepted request without downloading or executing P7."""

    report = DevSourceScanReport.model_validate(report.model_dump(mode="json"))
    market_data = tuple(
        ReplayMarketDataArtifact.model_validate(item.model_dump(mode="json"))
        for item in market_data
    )
    funding_schedules = tuple(
        FundingScheduleArtifact.model_validate(item.model_dump(mode="json"))
        for item in funding_schedules
    )
    funding_mark_supplements = tuple(
        FundingMarkSupplementArtifact.model_validate(item.model_dump(mode="json"))
        for item in funding_mark_supplements
    )
    requirements_plan_hash, transport_plan_hash, binding_messages = (
        _validate_authenticated_bindings(
            report=report,
            requirements=requirements,
            transport_plan=transport_plan,
            funding_schedules=funding_schedules,
        )
    )
    ready: list[ReplayReadyInput] = []
    issues: list[ReplayPreflightIssue] = []
    missing_mark_times_by_request: dict[str, tuple[datetime, ...]] = {}
    for accepted in report.accepted_requests:
        request = accepted.request
        request_issues: list[ReplayPreflightIssue] = []
        if binding_messages:
            request_issues.extend(
                _issue(request, "HASH_PROVENANCE", "INPUT_BINDING_MISMATCH", message)
                for message in binding_messages
            )
        if report.status != "COMPLETE":
            request_issues.append(
                _issue(request, "SCAN_INCOMPLETE", "SCAN_INCOMPLETE", "source scan is not COMPLETE")
            )
        schedule_matches = tuple(
            item
            for item in funding_schedules
            if item.symbol == request.request.armed.symbol
            and item.coverage_start <= request.start
            and item.coverage_end_exclusive >= request.end_exclusive
        )
        schedule = schedule_matches[0] if len(schedule_matches) == 1 else None
        duplicate_schedule = len(schedule_matches) > 1
        if duplicate_schedule:
            request_issues.append(
                _issue(
                    request,
                    "HASH_PROVENANCE",
                    "FUNDING_SCHEDULE_AMBIGUOUS",
                    "multiple Funding schedules match the request symbol",
                )
            )
        elif schedule is None:
            request_issues.append(
                _issue(
                    request,
                    "FUNDING_MISSING",
                    "FUNDING_SCHEDULE_MISSING",
                    "a versioned Funding schedule must cover the complete possible holding window",
                )
            )

        candle_matches = tuple(
            item
            for item in market_data
            if item.request_hash == request.request_hash and item.role == "CANDLE_ONE_MINUTE"
        )
        candle = candle_matches[0] if len(candle_matches) == 1 else None
        candles: pd.DataFrame | None = None
        if not candle_matches:
            request_issues.append(
                _issue(
                    request,
                    "CANDLE_MISSING",
                    "CANDLE_ARTIFACT_MISSING",
                    "1m Candle artifact is missing",
                )
            )
        elif len(candle_matches) > 1:
            request_issues.append(
                _issue(
                    request,
                    "HASH_PROVENANCE",
                    "CANDLE_ARTIFACT_AMBIGUOUS",
                    "multiple 1m artifacts match one request",
                )
            )
        elif candle is not None:
            try:
                if any(
                    response.symbol != request.request.armed.symbol for response in candle.responses
                ):
                    raise ReplayDataInputError("1m Candle symbol does not match request")
                loaded = load_replay_market_data_artifact(
                    project_dir=project_dir, request=request, artifact=candle
                )
                if not isinstance(loaded, pd.DataFrame):
                    raise ReplayDataInputError("Candle artifact loaded as the wrong data type")
                candles = loaded
                if (
                    len(candles) != request.expected_candle_count
                    or candles["open_time"].iloc[0] != request.start
                    or candles["close_time_exclusive"].iloc[-1] != request.end_exclusive
                ):
                    raise ReplayDataInputError("1m Candle boundary or count differs from request")
                if set(candles["symbol"]) != {request.request.armed.symbol}:
                    raise ReplayDataInputError("1m Candle symbol does not match request")
                if "source" not in candles or candles["source"].isna().any():
                    raise ReplayDataInputError("1m Candle source provenance is missing")
            except (OSError, ValueError, ReplayDataInputError) as error:
                request_issues.append(_data_error_issue(request, "CANDLE", error))

        required_times: tuple[datetime, ...] = ()
        funding: ReplayMarketDataArtifact | None = None
        used_supplement_hashes: tuple[str, ...] = ()
        used_supplement_warnings: tuple[str, ...] = ()
        schedule_covers = (
            schedule is not None
            and not duplicate_schedule
            and schedule.coverage_start <= request.start
            and schedule.coverage_end_exclusive >= request.end_exclusive
        )
        if schedule_covers and schedule is not None:
            required_times = tuple(
                value
                for value in schedule.settlement_times
                if request.start < value < request.end_exclusive - timedelta(minutes=1)
            )
            funding_matches = tuple(
                item
                for item in market_data
                if item.request_hash == request.request_hash and item.role == "FUNDING"
            )
            if required_times and not funding_matches:
                request_issues.append(
                    _issue(
                        request,
                        "FUNDING_MISSING",
                        "FUNDING_ARTIFACT_MISSING",
                        "Funding is required by the versioned schedule but its artifact is missing",
                    )
                )
            elif required_times and len(funding_matches) > 1:
                request_issues.append(
                    _issue(
                        request,
                        "HASH_PROVENANCE",
                        "FUNDING_ARTIFACT_AMBIGUOUS",
                        "multiple Funding artifacts match one request",
                    )
                )
            elif required_times:
                funding = funding_matches[0]
                try:
                    if any(
                        response.symbol != request.request.armed.symbol
                        for response in funding.responses
                    ):
                        raise ReplayDataInputError("Funding symbol does not match request")
                    loaded = load_replay_market_data_artifact(
                        project_dir=project_dir, request=request, artifact=funding
                    )
                    if not isinstance(loaded, FundingDataset):
                        raise ReplayDataInputError("Funding artifact loaded as the wrong data type")
                    observed = tuple(
                        item.settlement_time
                        for item in loaded.observations
                        if request.start
                        < item.settlement_time
                        < request.end_exclusive - timedelta(minutes=1)
                    )
                    missing = tuple(sorted(set(required_times) - set(observed)))
                    extra = tuple(sorted(set(observed) - set(required_times)))
                    if missing:
                        raise ReplayDataInputError(
                            "Funding records are missing scheduled settlements"
                        )
                    if extra:
                        raise ReplayDataInputError(
                            "Funding record falls outside the versioned schedule"
                        )
                    by_time = {item.settlement_time: item for item in loaded.observations}
                    missing_values = tuple(
                        value
                        for value in required_times
                        if (
                            (observation := by_time.get(value)) is None
                            or observation.rate is None
                            or observation.mark_price is None
                        )
                    )
                    if missing_values:
                        missing_mark_times = tuple(
                            value
                            for value in missing_values
                            if by_time.get(value) is None or by_time[value].mark_price is None
                        )
                        if missing_mark_times:
                            missing_mark_times_by_request[request.request_hash] = missing_mark_times
                            resolved = dict(by_time)
                            supplement_hashes: list[str] = []
                            supplement_warnings: list[str] = []
                            for value in missing_mark_times:
                                candidates = tuple(
                                    supplement
                                    for supplement in funding_mark_supplements
                                    if supplement.request_hash == request.request_hash
                                    and supplement.funding_artifact_hash == funding.artifact_hash
                                    and supplement.symbol == request.request.armed.symbol
                                    and supplement.funding_time == value
                                )
                                if len(candidates) != 1:
                                    raise ReplayDataInputError(
                                        "Funding mark_price supplement is missing or ambiguous at "
                                        f"{value.isoformat().replace('+00:00', 'Z')}"
                                    )
                                supplement = candidates[0]
                                mark = verify_funding_mark_supplement(
                                    project_dir=project_dir, artifact=supplement
                                )
                                if supplement.funding_rate != by_time[value].rate:
                                    raise ReplayDataInputError(
                                        "Funding mark supplement rate does not match "
                                        "original artifact"
                                    )
                                resolved[value] = by_time[value].model_copy(
                                    update={"mark_price": mark}
                                )
                                supplement_hashes.append(supplement.artifact_hash)
                                if supplement.warning is not None:
                                    supplement_warnings.append(supplement.warning)
                            by_time = resolved
                            missing_values = tuple(
                                value
                                for value in required_times
                                if (
                                    (observation := by_time.get(value)) is None
                                    or observation.rate is None
                                    or observation.mark_price is None
                                )
                            )
                            if not missing_values:
                                loaded = loaded.model_copy(
                                    update={
                                        "observations": tuple(
                                            by_time[item.settlement_time]
                                            for item in loaded.observations
                                        )
                                    }
                                )
                                used_supplement_hashes = tuple(sorted(supplement_hashes))
                                used_supplement_warnings = tuple(sorted(set(supplement_warnings)))
                                missing_mark_times_by_request.pop(request.request_hash, None)
                            else:
                                missing_mark_times_by_request[request.request_hash] = (
                                    missing_mark_times
                                )
                        if missing_values:
                            labels = ", ".join(
                                value.isoformat().replace("+00:00", "Z") for value in missing_values
                            )
                            raise ReplayDataInputError(
                                "Funding settlement observation is incomplete "
                                f"(rate/mark_price missing) at {labels}"
                            )
                except (OSError, ValueError, ReplayDataInputError) as error:
                    request_issues.append(_data_error_issue(request, "FUNDING", error))

        if request_issues:
            issues.extend(request_issues)
        elif candle is not None and candles is not None and schedule is not None:
            ready.append(
                _build_ready_input(
                    report=report,
                    request=request,
                    candle=candle,
                    funding=funding,
                    schedule=schedule,
                    required_times=required_times,
                    funding_mark_supplement_hashes=used_supplement_hashes,
                    funding_mark_warnings=used_supplement_warnings,
                )
            )

    ready.sort(key=lambda item: item.request_hash)
    issues.sort(key=lambda item: (item.request_hash, item.category, item.code))
    manifest_payload = {
        "schema_version": "p9-dev-replay-ready-manifest/0.1.0",
        "dataset_role": "DEV",
        "source_scan_report_hash": report.report_hash,
        "request_set_hash": report.request_set_hash,
        "accepted_request_count": len(report.accepted_requests),
        "ready_request_count": len(ready),
        "inputs": [item.model_dump(mode="json") for item in ready],
        "validation_locked_test_authorized": False,
    }
    manifest = ReplayReadyManifest.model_validate(
        {**manifest_payload, "manifest_hash": _hash(manifest_payload)}
    )
    categories_by_request = {
        category: {item.request_hash for item in issues if item.category == category}
        for category in (
            "CANDLE_MISSING",
            "FUNDING_MISSING",
            "HASH_PROVENANCE",
            "DUPLICATE_DISORDER_BOUNDARY",
            "SYMBOL_UTC",
        )
    }
    accepted_requests = tuple(item.request for item in report.accepted_requests)
    ready_requests = tuple(item.request for item in ready)
    required_start = min((item.start for item in accepted_requests), default=None)
    required_end = max((item.end_exclusive for item in accepted_requests), default=None)
    ready_start = min((item.start for item in ready_requests), default=None)
    ready_end = max((item.end_exclusive for item in ready_requests), default=None)

    def serialized_time(value: datetime | None) -> str | None:
        return value.isoformat().replace("+00:00", "Z") if value is not None else None

    can_start = bool(accepted_requests) and len(ready) == len(accepted_requests) and not issues
    gaps = []
    for category, label in (
        ("CANDLE_MISSING", "1m Candle 不完整"),
        ("FUNDING_MISSING", "Funding settlement 数据不完整"),
        ("HASH_PROVENANCE", "hash/provenance 异常"),
        ("DUPLICATE_DISORDER_BOUNDARY", "重复/乱序/边界异常"),
        ("SYMBOL_UTC", "symbol/UTC 异常"),
    ):
        if count := len(categories_by_request[category]):
            gaps.append(f"{label} {count} 个 request")
    if report.status != "COMPLETE":
        gaps.insert(0, "P9-Scan 尚未 COMPLETE")
    if missing_mark_times_by_request:
        gaps.insert(
            0,
            "Funding 必需 settlement 的 markPrice 缺失 "
            f"{len(missing_mark_times_by_request)} 个 request",
        )
    if binding_messages:
        gaps.insert(0, "report/requirements/transport 绑定校验失败")
    if not accepted_requests:
        gaps.append("没有 accepted request")
    summary_payload = {
        "schema_version": "p9-dev-replay-readiness/0.1.0",
        "source_scan_report_hash": report.report_hash,
        "accepted_request_count": len(accepted_requests),
        "replay_ready_count": len(ready),
        "not_ready_request_count": len(accepted_requests) - len(ready),
        "candle_missing_blocked_count": len(categories_by_request["CANDLE_MISSING"]),
        "funding_missing_blocked_count": len(categories_by_request["FUNDING_MISSING"]),
        "funding_mark_price_missing_request_count": len(missing_mark_times_by_request),
        "funding_mark_price_missing_event_count": sum(
            len(values) for values in missing_mark_times_by_request.values()
        ),
        "hash_provenance_blocked_count": len(categories_by_request["HASH_PROVENANCE"]),
        "duplicate_disorder_boundary_blocked_count": len(
            categories_by_request["DUPLICATE_DISORDER_BOUNDARY"]
        ),
        "symbol_utc_blocked_count": len(categories_by_request["SYMBOL_UTC"]),
        "symbol_count": len({item.request.armed.symbol for item in accepted_requests}),
        "required_coverage_start": serialized_time(required_start),
        "required_coverage_end_exclusive": serialized_time(required_end),
        "ready_data_coverage_start": serialized_time(ready_start),
        "ready_data_coverage_end_exclusive": serialized_time(ready_end),
        "can_start_dev_replay": can_start,
        "primary_missing": "无；可以正式启动 DEV replay" if can_start else "；".join(gaps),
        "issues": [item.model_dump(mode="json") for item in issues],
        "requirements_plan_hash": requirements_plan_hash,
        "transport_plan_hash": transport_plan_hash,
    }
    summary = ReplayReadinessSummary.model_validate(
        {**summary_payload, "summary_hash": _hash(summary_payload)}
    )
    return ReplayPrepResult(summary=summary, manifest=manifest)


def render_replay_readiness(summary: ReplayReadinessSummary) -> str:
    summary = ReplayReadinessSummary.model_validate(summary.model_dump(mode="json"))
    ready_coverage = (
        f"[{summary.ready_data_coverage_start.isoformat()}, "
        f"{summary.ready_data_coverage_end_exclusive.isoformat()})"
        if summary.ready_data_coverage_start is not None
        and summary.ready_data_coverage_end_exclusive is not None
        else "无完整数据覆盖"
    )
    required_coverage = (
        f"[{summary.required_coverage_start.isoformat()}, "
        f"{summary.required_coverage_end_exclusive.isoformat()})"
        if summary.required_coverage_start is not None
        and summary.required_coverage_end_exclusive is not None
        else "无"
    )
    return (
        "\n".join(
            (
                f"accepted requests：{summary.accepted_request_count}",
                f"可 replay：{summary.replay_ready_count}",
                f"未就绪/阻断：{summary.not_ready_request_count}",
                f"Candle 缺失阻断：{summary.candle_missing_blocked_count}",
                f"Funding 缺失阻断：{summary.funding_missing_blocked_count}",
                "Funding markPrice 缺失："
                f"{summary.funding_mark_price_missing_event_count} 个 event，影响 "
                f"{summary.funding_mark_price_missing_request_count} 个 request",
                f"hash/provenance 问题：{summary.hash_provenance_blocked_count}",
                "duplicate/disorder/boundary 问题："
                f"{summary.duplicate_disorder_boundary_blocked_count}",
                f"symbol/UTC 问题：{summary.symbol_utc_blocked_count}",
                f"symbols：{summary.symbol_count}",
                f"请求所需覆盖：{required_coverage}",
                f"就绪数据覆盖：{ready_coverage}",
                f"可正式启动 DEV replay：{'是' if summary.can_start_dev_replay else '否'}",
                f"主要缺口：{summary.primary_missing}",
            )
        )
        + "\n"
    )


class ReplayCheckpointEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_hash: Sha256
    input_hash: Sha256
    state: Literal["COMPLETE", "FAILED"]
    result_hash: Sha256 | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.state == "COMPLETE" and (
            self.result_hash is None or self.failure_reason is not None
        ):
            raise ValueError("completed replay requires only a result hash")
        if self.state == "FAILED" and (self.result_hash is not None or not self.failure_reason):
            raise ValueError("failed replay requires only an explicit reason")
        return self


class ReplayCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-dev-replay-checkpoint/0.1.0"] = "p9-dev-replay-checkpoint/0.1.0"
    ready_manifest_hash: Sha256
    request_count: int = Field(ge=0, strict=True)
    entries: tuple[ReplayCheckpointEntry, ...]
    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    checkpoint_hash: Sha256

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        hashes = tuple(item.request_hash for item in self.entries)
        if hashes != tuple(sorted(set(hashes))) or len(self.entries) > self.request_count:
            raise ValueError("checkpoint entries must be unique, canonical, and bounded")
        complete = sum(item.state == "COMPLETE" for item in self.entries)
        failed = sum(item.state == "FAILED" for item in self.entries)
        expected = (
            "COMPLETE"
            if complete == self.request_count
            else "FAILED"
            if self.request_count > 0 and failed == self.request_count
            else "PARTIAL"
        )
        if self.status != expected:
            raise ValueError("checkpoint status does not reconcile")
        payload = self.model_dump(mode="json", exclude={"checkpoint_hash"})
        if self.checkpoint_hash != _hash(payload):
            raise ValueError("checkpoint content hash mismatch")
        return self


class ReplayResumePlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    completed_request_hashes: tuple[Sha256, ...]
    pending_request_hashes: tuple[Sha256, ...]
    invalidated_request_hashes: tuple[Sha256, ...]
    prior_failures: tuple[ReplayCheckpointEntry, ...]


def build_resume_plan(
    manifest: ReplayReadyManifest, checkpoint: ReplayCheckpoint | None
) -> ReplayResumePlan:
    """Reuse only COMPLETE outcomes whose exact replay input hash is unchanged."""

    current = {item.request_hash: item.input_hash for item in manifest.inputs}
    prior = {item.request_hash: item for item in checkpoint.entries} if checkpoint else {}
    input_changed = any(
        request_hash in current and entry.input_hash != current[request_hash]
        for request_hash, entry in prior.items()
    )
    if (
        checkpoint is not None
        and checkpoint.ready_manifest_hash != manifest.manifest_hash
        and not input_changed
    ):
        return ReplayResumePlan(
            completed_request_hashes=(),
            pending_request_hashes=tuple(sorted(current)),
            invalidated_request_hashes=tuple(sorted(set(current) & set(prior))),
            prior_failures=(),
        )
    completed = tuple(
        sorted(
            request_hash
            for request_hash, input_hash in current.items()
            if (entry := prior.get(request_hash)) is not None
            and entry.state == "COMPLETE"
            and entry.input_hash == input_hash
        )
    )
    invalidated = tuple(
        sorted(
            request_hash
            for request_hash, input_hash in current.items()
            if (entry := prior.get(request_hash)) is not None and entry.input_hash != input_hash
        )
    )
    return ReplayResumePlan(
        completed_request_hashes=completed,
        pending_request_hashes=tuple(sorted(set(current) - set(completed))),
        invalidated_request_hashes=invalidated,
        prior_failures=tuple(
            sorted(
                (entry for entry in prior.values() if entry.state == "FAILED"),
                key=lambda item: item.request_hash,
            )
        ),
    )


def update_replay_checkpoint(
    *,
    manifest: ReplayReadyManifest,
    checkpoint: ReplayCheckpoint | None,
    request_hash: str,
    result_hash: str | None = None,
    failure_reason: str | None = None,
) -> ReplayCheckpoint:
    """Record one result atomically in memory, dropping stale input-bound entries."""

    current = {item.request_hash: item.input_hash for item in manifest.inputs}
    if request_hash not in current:
        raise ValueError("checkpoint request is not in the current ready manifest")
    if (result_hash is None) == (failure_reason is None):
        raise ValueError("provide exactly one of result_hash or failure_reason")
    checkpoint_entries = checkpoint.entries if checkpoint is not None else ()
    input_changed = checkpoint is not None and any(
        item.request_hash in current and item.input_hash != current[item.request_hash]
        for item in checkpoint_entries
    )
    entries = (
        {
            item.request_hash: item
            for item in checkpoint_entries
            if item.input_hash == current.get(item.request_hash)
        }
        if checkpoint is not None
        and (checkpoint.ready_manifest_hash == manifest.manifest_hash or input_changed)
        else {}
    )
    entries[request_hash] = ReplayCheckpointEntry(
        request_hash=request_hash,
        input_hash=current[request_hash],
        state="COMPLETE" if result_hash is not None else "FAILED",
        result_hash=result_hash,
        failure_reason=failure_reason,
    )
    ordered = tuple(sorted(entries.values(), key=lambda item: item.request_hash))
    complete = sum(item.state == "COMPLETE" for item in ordered)
    failed = sum(item.state == "FAILED" for item in ordered)
    status = (
        "COMPLETE"
        if complete == len(current)
        else "FAILED"
        if current and failed == len(current)
        else "PARTIAL"
    )
    payload = {
        "schema_version": "p9-dev-replay-checkpoint/0.1.0",
        "ready_manifest_hash": manifest.manifest_hash,
        "request_count": len(current),
        "entries": [item.model_dump(mode="json") for item in ordered],
        "status": status,
    }
    return ReplayCheckpoint.model_validate({**payload, "checkpoint_hash": _hash(payload)})


def write_replay_checkpoint(checkpoint: ReplayCheckpoint, path: Path) -> None:
    """Crash-safe replacement for the one small mutable replay state file."""

    checkpoint = ReplayCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
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


def load_replay_checkpoint(path: Path) -> ReplayCheckpoint:
    return ReplayCheckpoint.model_validate_json(path.read_bytes())


def write_replay_prep_artifacts(result: ReplayPrepResult, output_dir: Path) -> tuple[Path, ...]:
    """Write only immutable, content-addressed readiness and input artifacts."""

    output_dir = output_dir.resolve()
    documents: list[tuple[Path, bytes]] = []
    summary = result.summary
    manifest = result.manifest
    documents.extend(
        (
            (
                output_dir / f"{summary.summary_hash}.readiness.json",
                canonical_json_bytes(summary.model_dump(mode="json")),
            ),
            (
                output_dir / f"{summary.summary_hash}.readiness.txt",
                render_replay_readiness(summary).encode("utf-8"),
            ),
            (
                output_dir / f"{manifest.manifest_hash}.replay-ready-manifest.json",
                canonical_json_bytes(manifest.model_dump(mode="json")),
            ),
        )
    )
    documents.extend(
        (
            output_dir / "inputs" / f"{item.input_hash}.json",
            canonical_json_bytes(item.model_dump(mode="json")),
        )
        for item in manifest.inputs
    )
    paths = []
    for path, content in documents:
        if not path.resolve().is_relative_to(output_dir):
            raise ValueError("replay-prep output escapes its directory")
        if path.exists():
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError(f"existing replay-prep artifact changed: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            _publish_immutable(path, content)
        paths.append(path)
    return tuple(paths)


def write_replay_resume_plan(plan: ReplayResumePlan, output_dir: Path) -> Path:
    """Publish one content-addressed resume plan with the shared immutable primitive."""

    content = canonical_json_bytes(plan.model_dump(mode="json"))
    plan_hash = hashlib.sha256(content).hexdigest()
    destination = output_dir / f"{plan_hash}.resume-plan.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
