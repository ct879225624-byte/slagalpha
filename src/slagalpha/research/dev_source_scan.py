"""Memory-bounded DEV P3-P6 scan over verified monthly sources, without replay I/O."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self, cast

import pandas as pd
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.data.archive import (
    ArchiveDownloadManifest,
    ArchiveSpec,
    archive_path,
    sha256_file,
)
from slagalpha.data.klines import (
    CANDLE_SCHEMA_VERSION,
    INTERVAL_MILLISECONDS,
    PARSER_VERSION,
    ArchiveNormalizationManifest,
)
from slagalpha.domain.universe import (
    APPROXIMATE_TICK_SIZE_WARNING,
    ContractRegistry,
    UniverseSnapshot,
)
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.candle_inputs import (
    CandleInputError,
    CandlePartitionSource,
    contained_file,
)
from slagalpha.research.parameters import DevParameterVersion, require_parameter_plan_binding
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    ReplayDataInputError,
    Sha256,
    build_dev_replay_data_request,
)
from slagalpha.research.scan_plan import DevScanPlan, build_dev_scan_plan, model_hash
from slagalpha.research.scan_trade_plan import (
    ApproximateTickSizeImpact,
    _approximate_tick_impact,
    require_active_scan_rule,
)
from slagalpha.research.sensitivity import SensitivityPlan, require_dev_execution_inputs
from slagalpha.research.splits import ResearchSplitManifest, audit_research_inputs
from slagalpha.strategy.indicators import compute_indicator_frame
from slagalpha.strategy.pivots import (
    PIVOT_VERSION,
    PIVOT_ZONE_VERSION,
    ZONE_ATR_MULTIPLIER,
    PivotEvent,
    PivotType,
    PivotZone,
    _build_zone,
    detect_confirmed_pivots,
)
from slagalpha.strategy.plans import (
    EntryStopEvaluation,
    EntryStopRequest,
    TakeProfitEvaluation,
    build_entry_stop,
    build_take_profit,
)
from slagalpha.strategy.setup import (
    DailyContext,
    Direction,
    PullbackEvaluation,
    PullbackState,
    Regime,
    SetupContextEvaluation,
    SetupDecisionReason,
    SetupNotReadyError,
    evaluate_daily_context,
    evaluate_four_hour,
    evaluate_one_hour_pullback,
)
from slagalpha.strategy.triggers import (
    TriggerDecision,
    TriggerNotReadyError,
    TriggerSetupContext,
    TriggerType,
    evaluate_triggers,
)

_Interval = Literal["15m", "1h", "4h", "1d"]
_INTERVALS: tuple[_Interval, ...] = ("15m", "1h", "4h", "1d")
_WARMUP_START = datetime(2023, 2, 1, tzinfo=UTC)
_ALGORITHM_VERSION: Literal["dev-source-scan-algorithm/0.1.1"] = (
    "dev-source-scan-algorithm/0.1.1"
)


class AcceptedP6Request(BaseModel):
    """Compact accepted output with the approximate-tick effect retained."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: DevReplayDataRequest
    approximate_tick_impact: ApproximateTickSizeImpact | None

    @model_validator(mode="after")
    def validate_fallback(self) -> Self:
        approximate = APPROXIMATE_TICK_SIZE_WARNING in self.request.rule_warning_codes
        if approximate != (self.approximate_tick_impact is not None):
            raise ValueError("accepted request tick impact does not match its rule warning")
        return self


class ConfirmedTriggerSourcePartition(BaseModel):
    """Hash-only provenance for the verified source partitions visible to a record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval: _Interval
    period: str
    source_file_hash: Sha256
    normalized_content_hash: Sha256
    parquet_sha256: Sha256


class ConfirmedTriggerRecord(BaseModel):
    """Immutable P3-P6 evidence for one point-in-time confirmed trigger."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    schema_version: Literal["confirmed-trigger-record/0.1.0"] = (
        "confirmed-trigger-record/0.1.0"
    )
    symbol: str
    logical_signal_id: Sha256
    direction: Direction
    primary_trigger: TriggerType
    confirmation_open_time: datetime
    confirmation_close_time: datetime
    confirmation_close_price: Decimal
    trigger_decision: TriggerDecision
    setup_context: SetupContextEvaluation
    visible_pivots: tuple[PivotEvent, ...]
    visible_zones: tuple[PivotZone, ...]
    visible_hourly_pivots: tuple[PivotEvent, ...]
    visible_hourly_zones: tuple[PivotZone, ...]
    atr_at_confirmation: Decimal
    atr_timestamp: datetime
    invalidation_price: Decimal
    structure_context: dict[str, Any]
    tick_size: Decimal
    tick_size_rule_content_hash: Sha256
    tick_size_verification_status: str
    tick_size_confidence: str
    tick_size_warning_codes: tuple[str, ...]
    approximate_tick_impact: ApproximateTickSizeImpact | None
    entry_stop_request: EntryStopRequest
    baseline_entry_stop: EntryStopEvaluation
    baseline_take_profit: TakeProfitEvaluation | None
    baseline_data_request: DevReplayDataRequest | None
    baseline_p6_status: Literal[
        "REJECTED_ENTRY_STOP", "REJECTED_TAKE_PROFIT",
        "REJECTED_REQUEST_BOUNDARY", "ACCEPTED_PLAN"
    ]
    baseline_rejection_reasons: tuple[str, ...]
    universe_content_hash: Sha256
    source_inventory_hash: Sha256
    source_partitions: tuple[ConfirmedTriggerSourcePartition, ...]
    scan_plan_hash: Sha256
    split_hash: Sha256
    sensitivity_plan_hash: Sha256
    parameter_content_hash: Sha256
    registry_content_hash: Sha256
    algorithm_version: str
    strategy_version: str
    trigger_version: str
    setup_version: str
    trade_plan_version: str
    pivot_version: str
    zone_version: str
    record_hash: Sha256

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.symbol != self.trigger_decision.symbol or self.symbol != self.setup_context.symbol:
            raise ValueError("confirmed trigger symbol does not match bound evidence")
        if self.logical_signal_id != self.trigger_decision.logical_signal_id:
            raise ValueError("confirmed trigger signal id does not match TriggerDecision")
        if not self.trigger_decision.eligible_for_plan:
            raise ValueError("ledger records must contain eligible TriggerDecisions")
        if self.primary_trigger is not self.trigger_decision.primary_trigger:
            raise ValueError("primary trigger does not match TriggerDecision")
        if self.entry_stop_request.logical_signal_id != self.logical_signal_id:
            raise ValueError("Entry/Stop request does not match logical signal")
        if self.entry_stop_request.confirmation_close != self.confirmation_close_time:
            raise ValueError("Entry/Stop request does not match confirmation close")
        if self.baseline_entry_stop.request != self.entry_stop_request:
            raise ValueError("baseline Entry/Stop does not match its request")
        if self.baseline_p6_status == "ACCEPTED_PLAN":
            if (self.baseline_take_profit is None or not self.baseline_take_profit.accepted
                    or self.baseline_data_request is None):
                raise ValueError("accepted ledger record lacks complete P6 evidence")
        elif self.baseline_data_request is not None:
            raise ValueError("rejected ledger record cannot contain a data request")
        if self.baseline_p6_status == "REJECTED_ENTRY_STOP" and self.baseline_entry_stop.accepted:
            raise ValueError("Entry/Stop rejection status contradicts its evaluation")
        if self.baseline_p6_status == "REJECTED_TAKE_PROFIT":
            if self.baseline_entry_stop.accepted and (
                self.baseline_take_profit is None or self.baseline_take_profit.accepted
            ):
                raise ValueError("Take-profit rejection status contradicts its evaluation")
        if self.baseline_p6_status == "REJECTED_REQUEST_BOUNDARY":
            if (not self.baseline_entry_stop.accepted or self.baseline_take_profit is None
                    or not self.baseline_take_profit.accepted):
                raise ValueError("request-boundary rejection lacks accepted P6 evidence")
        if self.tick_size != self.entry_stop_request.tick_size:
            raise ValueError("tick-size context does not match Entry/Stop request")
        payload = self.model_dump(mode="json", exclude={"record_hash"})
        if self.record_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("confirmed trigger record hash mismatch")
        return self


class ConfirmedTriggerLedger(BaseModel):
    """Content-addressed immutable ledger of all confirmed P5 triggers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["confirmed-trigger-ledger/0.1.0"] = (
        "confirmed-trigger-ledger/0.1.0"
    )
    scan_plan_hash: Sha256
    split_hash: Sha256
    sensitivity_plan_hash: Sha256
    parameter_content_hash: Sha256
    registry_content_hash: Sha256
    source_inventory_hash: Sha256
    algorithm_version: str
    record_count: int = Field(ge=0, strict=True)
    scanned_symbols: tuple[str, ...]
    symbol_confirmed_counts: dict[str, int]
    records: tuple[ConfirmedTriggerRecord, ...]
    ledger_hash: Sha256

    @model_validator(mode="after")
    def validate_ledger(self) -> Self:
        if self.record_count != len(self.records):
            raise ValueError("ledger record count does not reconcile")
        keys = tuple(
            (item.confirmation_close_time, item.symbol, item.logical_signal_id)
            for item in self.records
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("ledger records must be unique and canonical")
        if self.scanned_symbols != tuple(sorted(set(self.scanned_symbols))):
            raise ValueError("ledger scanned symbols must be unique and canonical")
        expected_counts = {
            symbol: sum(item.symbol == symbol for item in self.records)
            for symbol in self.scanned_symbols
        }
        expected_counts = dict(sorted(expected_counts.items()))
        if self.symbol_confirmed_counts != expected_counts:
            raise ValueError("ledger symbol counts do not reconcile")
        if any(
            item.scan_plan_hash != self.scan_plan_hash
            or item.split_hash != self.split_hash
            or item.sensitivity_plan_hash != self.sensitivity_plan_hash
            or item.parameter_content_hash != self.parameter_content_hash
            or item.registry_content_hash != self.registry_content_hash
            or item.source_inventory_hash != self.source_inventory_hash
            or item.algorithm_version != self.algorithm_version
            for item in self.records
        ):
            raise ValueError("ledger record provenance is not uniform")
        payload = self.model_dump(mode="json", exclude={"ledger_hash"})
        if self.ledger_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("confirmed trigger ledger hash mismatch")
        return self


class DevScanFunnel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scan_slot_count: int = Field(ge=0, strict=True)
    setup_evaluated_count: int = Field(ge=0, strict=True)
    setup_not_ready_count: int = Field(ge=0, strict=True)
    setup_eligible_count: int = Field(ge=0, strict=True)
    trigger_evaluated_count: int = Field(ge=0, strict=True)
    trigger_not_ready_count: int = Field(ge=0, strict=True)
    trigger_confirmed_count: int = Field(ge=0, strict=True)
    entry_stop_rejected_count: int = Field(ge=0, strict=True)
    take_profit_rejected_count: int = Field(ge=0, strict=True)
    request_boundary_rejected_count: int = Field(ge=0, strict=True)
    accepted_count: int = Field(ge=0, strict=True)
    source_error_slot_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.scan_slot_count != (
            self.setup_evaluated_count
            + self.setup_not_ready_count
            + self.source_error_slot_count
        ):
            raise ValueError("scan slots do not reconcile with setup/source outcomes")
        if self.trigger_evaluated_count + self.trigger_not_ready_count != self.setup_eligible_count:
            raise ValueError("eligible setups do not reconcile with trigger outcomes")
        if self.trigger_confirmed_count > self.trigger_evaluated_count:
            raise ValueError("confirmed triggers exceed evaluated triggers")
        if self.trigger_confirmed_count != (
            self.entry_stop_rejected_count
            + self.take_profit_rejected_count
            + self.request_boundary_rejected_count
            + self.accepted_count
        ):
            raise ValueError("confirmed triggers do not reconcile with P6 outcomes")
        return self


class DevMarketDataRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...]
    one_minute_rows_before_overlap_dedup: int = Field(ge=0, strict=True)
    one_minute_rows_after_overlap_dedup: int = Field(ge=0, strict=True)
    funding_windows_before_overlap_dedup: int = Field(ge=0, strict=True)
    funding_windows_after_overlap_dedup: int = Field(ge=0, strict=True)
    merged_window_minutes: int = Field(ge=0, strict=True)


class DevSourceScanReport(BaseModel):
    """Content-addressed scan result; it authorizes neither download nor replay."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    schema_version: Literal["dev-source-scan/0.1.0"] = "dev-source-scan/0.1.0"
    scan_plan_hash: Sha256
    split_hash: Sha256
    sensitivity_plan_hash: Sha256
    parameter_content_hash: Sha256
    registry_content_hash: Sha256
    source_inventory_hash: Sha256
    source_validation_mode: Literal["BOUND_RAW_AND_PARQUET_SHA256"]
    source_partition_count: int = Field(ge=0, strict=True)
    validated_source_partition_count: int = Field(ge=0, strict=True)
    scan_start: datetime
    scan_end_exclusive: datetime
    universe_symbol_count: int = Field(gt=0, strict=True)
    scanned_symbols: tuple[str, ...]
    accepted_symbols: tuple[str, ...]
    funnel: DevScanFunnel
    reason_counts: dict[str, int]
    fallback_rule_slot_count: int = Field(ge=0, strict=True)
    fallback_price_plan_count: int = Field(ge=0, strict=True)
    approximate_price_observation_count: int = Field(ge=0, strict=True)
    approximate_outcome_warning_count: int = Field(ge=0, strict=True)
    max_approximate_tick_adjustment_bps: Decimal | None
    warning_codes: tuple[str, ...]
    accepted_requests: tuple[AcceptedP6Request, ...]
    request_set_hash: Sha256
    market_data_requirement: DevMarketDataRequirement
    anomalies: tuple[str, ...]
    status: Literal["COMPLETE", "PARTIAL"]
    download_authorized: Literal[False] = False
    replay_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    report_hash: Sha256

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.scan_start.tzinfo is None or self.scan_start.utcoffset() != timedelta(0):
            raise ValueError("scan start must use UTC")
        if (
            self.scan_end_exclusive.tzinfo is None
            or self.scan_end_exclusive.utcoffset() != timedelta(0)
        ):
            raise ValueError("scan end must use UTC")
        if self.scanned_symbols != tuple(sorted(set(self.scanned_symbols))):
            raise ValueError("scanned symbols must be unique and canonical")
        if self.accepted_symbols != tuple(sorted(set(self.accepted_symbols))):
            raise ValueError("accepted symbols must be unique and canonical")
        if self.reason_counts != dict(sorted(self.reason_counts.items())):
            raise ValueError("scan reasons must be canonical")
        if self.warning_codes != tuple(sorted(set(self.warning_codes))):
            raise ValueError("scan warnings must be unique and canonical")
        if self.funnel.accepted_count != len(self.accepted_requests):
            raise ValueError("accepted request count does not reconcile")
        keys = tuple(
            (item.request.start, item.request.request.armed.symbol)
            for item in self.accepted_requests
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("accepted requests must be unique and canonical")
        request_payload = [item.model_dump(mode="json") for item in self.accepted_requests]
        request_document = {"accepted_requests": request_payload}
        if self.request_set_hash != hashlib.sha256(
            canonical_json_bytes(request_document)
        ).hexdigest():
            raise ValueError("accepted request set hash mismatch")
        incomplete = bool(self.anomalies or self.funnel.source_error_slot_count)
        if (self.status == "PARTIAL") != incomplete:
            raise ValueError("scan status does not match incomplete outcomes")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("DEV source scan report hash mismatch")
        return self


@dataclass(frozen=True)
class _PreparedStream:
    candles: pd.DataFrame
    indicators: pd.DataFrame
    close_ns: tuple[int, ...]
    pivots: tuple[PivotEvent, ...]
    pivot_times: tuple[datetime, ...]
    pivot_open_times: tuple[datetime, ...]
    source_count: int


class ScanExecutionBinding(BaseModel):
    """Stage A execution identity, separate from the unchanged scan-report schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executor_version: Literal["p9-rescan-stage-a/0.1.0"]
    scanner_algorithm_version: Literal["dev-source-scan-algorithm/0.1.1"]
    candidate_hash: Sha256
    parameter_content_hash: Sha256
    sensitivity_plan_hash: Sha256
    scan_plan_hash: Sha256
    split_hash: Sha256
    registry_hash: Sha256
    source_inventory_hash: Sha256
    universe_hash: Sha256
    source_code_hash: Sha256
    environment_lock_hash: Sha256
    threshold_policy_hash: Sha256
    source_catalog_hashes: dict[str, Sha256]


def source_catalog_hash(catalog: dict[_Interval, tuple[CandlePartitionSource, ...]]) -> str:
    return hashlib.sha256(canonical_json_bytes({
        "sources": [source.model_dump(mode="json")
                    for interval in _INTERVALS for source in catalog[interval]],
    })).hexdigest()


def validate_execution_checkpoint(
    document: dict[str, Any], binding: ScanExecutionBinding,
) -> DevScanFunnel:
    """Reject legacy, incomplete, mixed-candidate or unreconciled symbol outputs."""
    if document.get("execution_binding") != binding.model_dump(mode="json"):
        raise CandleInputError("checkpoint execution binding mismatch")
    diagnostics = document.get("diagnostics")
    if not isinstance(diagnostics, dict) or set(diagnostics) != {
        "funnel", "pivot_counts", "zone_observation_counts", "structure_observation_slots",
    }:
        raise CandleInputError("checkpoint diagnostics missing or unsupported")
    funnel = DevScanFunnel.model_validate(diagnostics["funnel"])
    if (document.get("anomaly") is not None or document.get("scanned") is not True
            or funnel.source_error_slot_count
            or funnel.scan_slot_count != document["symbol_slot_count"]
            or document["validated_source_partition_count"] != document["source_partition_count"]):
        raise CandleInputError("checkpoint source/slot reconciliation mismatch")
    if any(document["outcome_counts"].get(key, 0) != value
           for key, value in funnel.model_dump().items() if key != "scan_slot_count"):
        raise CandleInputError("checkpoint funnel reconciliation mismatch")
    records = tuple(ConfirmedTriggerRecord.model_validate(item)
                    for item in document["confirmed_triggers"])
    accepted = tuple(AcceptedP6Request.model_validate(item)
                     for item in document["accepted_requests"])
    if (len(records) != funnel.trigger_confirmed_count
            or len({item.logical_signal_id for item in records}) != len(records)
            or len(accepted) != funnel.accepted_count):
        raise CandleInputError("checkpoint ledger reconciliation mismatch")
    statuses = Counter(item.baseline_p6_status for item in records)
    if (statuses["REJECTED_ENTRY_STOP"] != funnel.entry_stop_rejected_count
            or statuses["REJECTED_TAKE_PROFIT"] != funnel.take_profit_rejected_count
            or statuses["REJECTED_REQUEST_BOUNDARY"] != funnel.request_boundary_rejected_count
            or statuses["ACCEPTED_PLAN"] != funnel.accepted_count):
        raise CandleInputError("checkpoint P6 reconciliation mismatch")
    for record in records:
        if (record.symbol != document["symbol"]
                or record.parameter_content_hash != binding.parameter_content_hash
                or record.scan_plan_hash != binding.scan_plan_hash
                or record.split_hash != binding.split_hash
                or record.sensitivity_plan_hash != binding.sensitivity_plan_hash
                or record.registry_content_hash != binding.registry_hash
                or record.source_inventory_hash != binding.source_inventory_hash
                or record.algorithm_version != binding.scanner_algorithm_version):
            raise CandleInputError("checkpoint ledger candidate/source binding mismatch")
        if (record.atr_timestamp != record.confirmation_close_time
                or any(item.confirmed_at > record.confirmation_close_time
                       for item in record.visible_pivots)
                or any(item.confirmed_at > record.confirmation_close_time
                       for item in record.visible_zones)
                or any(item.confirmed_at > record.confirmation_close_time
                       for item in record.visible_hourly_pivots)
                or any(item.confirmed_at > record.confirmation_close_time
                       for item in record.visible_hourly_zones)):
            raise CandleInputError("checkpoint point-in-time violation")
    expected = [AcceptedP6Request(request=item.baseline_data_request,
                                 approximate_tick_impact=item.approximate_tick_impact)
                for item in records if item.baseline_data_request is not None]
    if list(accepted) != expected:
        raise CandleInputError("checkpoint accepted payload reconciliation mismatch")
    return funnel


@dataclass
class _IncrementalZoneState:
    """Produce the same prefix zones as merge_pivot_zones without rescanning history."""

    next_index: int = 0
    completed: list[PivotZone] = field(default_factory=list)
    current: dict[PivotType, list[PivotEvent]] = field(default_factory=dict)

    def available(
        self,
        pivots: tuple[PivotEvent, ...],
        count: int,
        since: datetime,
    ) -> tuple[PivotZone, ...]:
        if count < self.next_index:
            raise CandleInputError("pivot availability must advance monotonically")
        for pivot in pivots[self.next_index:count]:
            members = self.current.setdefault(pivot.kind, [])
            if members:
                threshold = ZONE_ATR_MULTIPLIER * pivot.atr_at_confirmation
                if abs(pivot.price - members[-1].price) >= threshold:
                    self.completed.append(_build_zone(members))
                    members = []
                    self.current[pivot.kind] = members
            members.append(pivot)
        self.next_index = count
        active = [_build_zone(members) for members in self.current.values() if members]
        recent = (
            zone
            for zone in (*self.completed, *active)
            if zone.members[-1].pivot_time >= since
        )
        return tuple(sorted(
            recent,
            key=lambda zone: (
                zone.confirmed_at,
                zone.interval,
                zone.kind.value,
                zone.zone_id,
            ),
        ))


def _periods(start: str, end: str) -> tuple[str, ...]:
    year, month = (int(part) for part in start.split("-"))
    values = []
    while f"{year:04d}-{month:02d}" <= end:
        values.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(values)


def _aligned_stream_start(lifecycle_start: datetime, interval: _Interval) -> datetime:
    step_ms = INTERVAL_MILLISECONDS[interval]
    lifecycle_ms = int(lifecycle_start.timestamp() * 1000)
    aligned_ms = ((lifecycle_ms + step_ms - 1) // step_ms) * step_ms
    return datetime.fromtimestamp(aligned_ms / 1000, tz=UTC)


def _close_times_ns(values: pd.Series) -> tuple[int, ...]:
    return tuple(pd.DatetimeIndex(values).as_unit("ns").asi8.tolist())


def _symbol_checkpoint_path(
    project_dir: Path,
    checkpoint_dir: Path,
    binding_hash: str,
    symbol: str,
) -> Path:
    path = checkpoint_dir / binding_hash / f"{symbol}.json"
    if not path.resolve().is_relative_to(project_dir.resolve()):
        raise CandleInputError("DEV source scan checkpoint path escapes project")
    for parent in (checkpoint_dir, checkpoint_dir / binding_hash, path):
        if parent.is_symlink() or parent.is_junction():
            raise CandleInputError("DEV source scan checkpoint path cannot be linked")
    return path


def _write_symbol_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    document = {
        **payload,
        "checkpoint_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }
    content = canonical_json_bytes(document)
    if path.exists() and (path.is_symlink() or path.read_bytes() != content):
        raise CandleInputError("existing DEV source scan checkpoint changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(path, content)


def _read_symbol_checkpoint(
    path: Path,
    *,
    binding_hash: str,
    symbol: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = cast(dict[str, Any], json.loads(path.read_bytes()))
        claimed_hash = document.pop("checkpoint_hash")
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as error:
        raise CandleInputError("invalid DEV source scan checkpoint") from error
    if (
        document.get("schema_version") != "dev-source-symbol-scan/0.1.0"
        or document.get("binding_hash") != binding_hash
        or document.get("symbol") != symbol
        or claimed_hash
        != hashlib.sha256(canonical_json_bytes(document)).hexdigest()
    ):
        raise CandleInputError("DEV source scan checkpoint binding or hash mismatch")
    return document


def _load_symbol_sources(
    project_dir: Path,
    symbol: str,
    *,
    final_period: str,
) -> dict[_Interval, tuple[CandlePartitionSource, ...]]:
    """Load one symbol's exact receipt bindings without retaining the whole inventory."""
    manifests = project_dir / "data/manifests"
    catalog: dict[_Interval, tuple[CandlePartitionSource, ...]] = {}
    for interval in _INTERVALS:
        by_period: dict[str, list[ArchiveNormalizationManifest]] = defaultdict(list)
        root = manifests / "archive_normalization" / symbol / interval
        for path in root.glob("*.json"):
            item = ArchiveNormalizationManifest.model_validate_json(path.read_bytes())
            if "2023-02" <= item.period <= final_period:
                by_period[item.period].append(item)
        if not by_period:
            raise CandleInputError(f"no DEV source partitions for {symbol}/{interval}")
        sources = []
        for period in _periods(min(by_period), final_period):
            matches = by_period.get(period, [])
            if len(matches) != 1:
                detail = f"{symbol}/{interval}/{period}"
                raise CandleInputError(
                    f"expected one source for {detail}; found {len(matches)}"
                )
            normalization = matches[0]
            year, month = (int(part) for part in period.split("-"))
            download = ArchiveDownloadManifest.model_validate_json(
                (
                    manifests
                    / "archive_download"
                    / symbol
                    / interval
                    / f"{normalization.source_file_hash}.json"
                ).read_bytes()
            )
            sources.append(CandlePartitionSource(
                spec=ArchiveSpec(
                    symbol=symbol, interval=interval, year=year, month=month
                ),
                download=download,
                normalization=normalization,
            ))
        catalog[interval] = tuple(sources)
    return catalog


def _source_inventory(
    project_dir: Path,
    symbols: tuple[str, ...],
    *,
    final_period: str,
) -> tuple[str, int, dict[str, int]]:
    entries = []
    partition_counts = {}
    for symbol in symbols:
        catalog = _load_symbol_sources(
            project_dir, symbol, final_period=final_period
        )
        sources = [
            source.model_dump(mode="json")
            for interval in _INTERVALS
            for source in catalog[interval]
        ]
        partition_counts[symbol] = len(sources)
        entries.append({
            "symbol": symbol,
            "source_hash": hashlib.sha256(
                canonical_json_bytes({"sources": sources})
            ).hexdigest(),
            "partition_count": len(sources),
        })
    document = {"symbols": entries}
    return (
        hashlib.sha256(canonical_json_bytes(document)).hexdigest(),
        sum(partition_counts.values()),
        partition_counts,
    )


def _load_bound_candle_partition(
    project_dir: Path,
    source: CandlePartitionSource,
) -> pd.DataFrame:
    """Read bytes bound to verified receipts without repeating archive normalization."""
    source = CandlePartitionSource.model_validate(source.model_dump(mode="json"))
    spec, download, manifest = source.spec, source.download, source.normalization
    raw_relative = archive_path(Path("data/raw"), spec).as_posix()
    raw = contained_file(project_dir, raw_relative)
    parquet = contained_file(
        project_dir, "data/normalized/" + manifest.output_relative_path
    )
    if (
        raw.stat().st_size != download.file_size
        or sha256_file(raw) != download.actual_sha256
    ):
        raise CandleInputError("raw archive no longer matches its verified receipt")
    if sha256_file(parquet) != manifest.parquet_sha256:
        raise CandleInputError("Parquet file no longer matches its normalization receipt")
    table = pq.ParquetFile(parquet).read()
    metadata = table.schema.metadata or {}
    expected_metadata = {
        b"candle_schema_version": CANDLE_SCHEMA_VERSION,
        b"parser_version": PARSER_VERSION,
        b"normalized_content_hash": manifest.normalized_content_hash,
        b"source_file_hash": manifest.source_file_hash,
        b"exchange": "BINANCE_USDM",
        b"symbol": spec.symbol,
        b"interval": spec.interval,
    }
    if any(metadata.get(key) != value.encode() for key, value in expected_metadata.items()):
        raise CandleInputError("Parquet metadata does not match its source receipt")
    frame = table.to_pandas()
    if "ingested_at" not in frame:
        raise CandleInputError("Parquet is missing its ingestion timestamp")
    frame = frame.drop(columns="ingested_at")
    frame["exchange"] = "BINANCE_USDM"
    frame["symbol"] = spec.symbol
    frame["interval"] = spec.interval
    if (
        len(frame) != manifest.normalized_row_count
        or frame.empty
        or frame["open_time"].iloc[0] != manifest.first_open_time
        or frame["open_time"].iloc[-1] != manifest.last_open_time
    ):
        raise CandleInputError("Parquet row boundaries do not match its receipt")
    return frame


def _prepare_stream(
    project_dir: Path,
    sources: tuple[CandlePartitionSource, ...],
    *,
    end_exclusive: datetime,
    lifecycle_start: datetime,
    pivot_window: tuple[int, int],
) -> _PreparedStream:
    chunks = [_load_bound_candle_partition(project_dir, item) for item in sources]
    interval = cast(_Interval, sources[0].spec.interval)
    step_ms = INTERVAL_MILLISECONDS[interval]
    history_start = max(
        _WARMUP_START,
        _aligned_stream_start(lifecycle_start, interval),
        sources[0].normalization.first_open_time,
    )
    candles = pd.concat(chunks, ignore_index=True)
    candles = candles.loc[
        (candles["open_time"] >= history_start)
        & (candles["close_time_exclusive"] <= end_exclusive)
    ].reset_index(drop=True)
    if candles.empty:
        raise CandleInputError("verified source stream has no visible DEV history")
    step = pd.Timedelta(milliseconds=step_ms)
    expected = pd.date_range(start=history_start, periods=len(candles), freq=step)
    if not pd.DatetimeIndex(candles["open_time"]).as_unit("ms").equals(expected.as_unit("ms")):
        raise CandleInputError("verified source stream is not continuous from its declared origin")
    indicators = compute_indicator_frame(candles)
    pivots = (
        detect_confirmed_pivots(
            candles, indicators, interval, left=pivot_window[0], right=pivot_window[1],
        )
        if interval in ("15m", "1h")
        else ()
    )
    return _PreparedStream(
        candles=candles,
        indicators=indicators,
        close_ns=_close_times_ns(candles["close_time_exclusive"]),
        pivots=pivots,
        pivot_times=tuple(item.confirmed_at for item in pivots),
        pivot_open_times=tuple(item.pivot_time for item in pivots),
        source_count=len(sources),
    )


def _visible_end(stream: _PreparedStream, at: datetime) -> int:
    return bisect_right(stream.close_ns, pd.Timestamp(at).value)


def _slice(
    stream: _PreparedStream,
    end: int,
    rows: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = 0 if rows is None else max(0, end - rows)
    return stream.candles.iloc[start:end], stream.indicators.iloc[start:end]


def _hourly_episode_slice(
    stream: _PreparedStream, end: int, direction: Direction,
) -> tuple[pd.DataFrame, pd.DataFrame, PullbackEvaluation]:
    window = min(end, 22)
    while True:
        candles, indicators = _slice(stream, end, window)
        result = evaluate_one_hour_pullback(
            candles, indicators, symbol=str(candles["symbol"].iloc[-1]), direction=direction,
        )
        starts_at_edge = (
            result.state in (PullbackState.SHALLOW, PullbackState.STANDARD)
            and result.episode_start == pd.Timestamp(candles["open_time"].iloc[0]).to_pydatetime()
        )
        if not starts_at_edge or window == end:
            return candles, indicators, result
        window = min(end, window * 2)


def _setup_at(
    streams: dict[_Interval, _PreparedStream],
    ends: dict[_Interval, int],
    *,
    compression_threshold: float,
) -> SetupContextEvaluation:
    four_candles, four_indicators = _slice(streams["4h"], ends["4h"], 12)
    four = evaluate_four_hour(
        four_candles, four_indicators, compression_threshold=compression_threshold,
    )
    symbol = str(four_candles["symbol"].iloc[-1])
    if four.direction is Direction.NEUTRAL:
        return SetupContextEvaluation(
            symbol=symbol,
            four_hour=four,
            daily=None,
            one_hour=None,
            eligible_for_trigger=False,
            decision_reason=SetupDecisionReason.FOUR_HOUR_NEUTRAL,
        )
    if four.regime is Regime.COMPRESSED:
        return SetupContextEvaluation(
            symbol=symbol,
            four_hour=four,
            daily=None,
            one_hour=None,
            eligible_for_trigger=False,
            decision_reason=SetupDecisionReason.FOUR_HOUR_COMPRESSED,
        )
    daily_candles, daily_indicators = _slice(streams["1d"], ends["1d"], 6)
    daily = evaluate_daily_context(
        daily_candles, daily_indicators, direction=four.direction,
    )
    if daily.context in (DailyContext.BLOCK_LONG, DailyContext.BLOCK_SHORT):
        reason = (
            SetupDecisionReason.DAILY_BLOCK_LONG
            if daily.context is DailyContext.BLOCK_LONG
            else SetupDecisionReason.DAILY_BLOCK_SHORT
        )
        return SetupContextEvaluation(
            symbol=symbol,
            four_hour=four,
            daily=daily,
            one_hour=None,
            eligible_for_trigger=False,
            decision_reason=reason,
        )
    _, _, hourly = _hourly_episode_slice(
        streams["1h"], ends["1h"], four.direction,
    )
    return SetupContextEvaluation(
        symbol=symbol,
        four_hour=four,
        daily=daily,
        one_hour=hourly,
        eligible_for_trigger=hourly.eligible_for_trigger,
        decision_reason=SetupDecisionReason.ONE_HOUR_EVALUATED,
    )


def _available_structure(
    stream: _PreparedStream,
    at: datetime,
    state: _IncrementalZoneState,
    *,
    pivot_since: datetime,
    zone_since: datetime,
) -> tuple[tuple[PivotEvent, ...], tuple[PivotZone, ...]]:
    count = bisect_right(stream.pivot_times, at)
    first = bisect_left(stream.pivot_open_times, pivot_since)
    return stream.pivots[first:count], state.available(
        stream.pivots, count, zone_since
    )


def _merged_windows(
    requests: tuple[AcceptedP6Request, ...],
) -> tuple[int, int]:
    grouped: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for item in requests:
        grouped[item.request.request.armed.symbol].append(
            (item.request.start, item.request.end_exclusive)
        )
    count = 0
    minutes = 0
    for windows in grouped.values():
        windows.sort()
        merged: list[list[datetime]] = []
        for start, end in windows:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        count += len(merged)
        minutes += sum(int((end - start).total_seconds() // 60) for start, end in merged)
    return count, minutes


def build_dev_source_scan_report(
    *,
    project_dir: Path,
    scan_plan: DevScanPlan,
    split: ResearchSplitManifest,
    plan: SensitivityPlan,
    parameter: DevParameterVersion,
    snapshots: tuple[UniverseSnapshot, ...],
    registry: ContractRegistry,
    checkpoint_dir: Path | None = None,
    progress: Callable[[int, int, str, int, int], None] | None = None,
    confirmed_trigger_sink: Callable[[ConfirmedTriggerRecord], None] | None = None,
    execution_binding: ScanExecutionBinding | None = None,
    symbol_result_sink: Callable[[dict[str, Any]], None] | None = None,
) -> DevSourceScanReport:
    """Run the frozen baseline once per verified stream and once per planned DEV slot."""
    scan_plan = DevScanPlan.model_validate(scan_plan.model_dump(mode="json"))
    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    snapshots = tuple(UniverseSnapshot.model_validate(item.model_dump(mode="json"))
                      for item in snapshots)
    registry = ContractRegistry.model_validate(registry.model_dump(mode="json"))
    require_parameter_plan_binding(parameter, plan)
    rebuilt_scan_plan = build_dev_scan_plan(
        split=split, plan=plan, parameter=parameter, snapshots=snapshots
    )
    if rebuilt_scan_plan != scan_plan:
        raise CandleInputError("source scan plan does not match the frozen context")
    require_dev_execution_inputs(
        plan,
        audit_research_inputs(split=split, snapshots=snapshots, registry=registry),
    )
    symbols = tuple(sorted({symbol for day in scan_plan.days for symbol in day.symbols}))
    final_period = scan_plan.days[-1].end_exclusive.strftime("%Y-%m")
    by_date = {item.selected_at.date(): item for item in snapshots}
    inventory_hash, partition_count, partition_counts = _source_inventory(
        project_dir, symbols, final_period=final_period
    )
    registry_hash = model_hash(registry)
    if execution_binding is not None:
        execution_binding = ScanExecutionBinding.model_validate(
            execution_binding.model_dump(mode="json")
        )
        expected_binding = {
            "scanner_algorithm_version": _ALGORITHM_VERSION,
            "candidate_hash": parameter.candidate.candidate_hash,
            "parameter_content_hash": parameter.content_hash,
            "sensitivity_plan_hash": plan.plan_hash,
            "scan_plan_hash": scan_plan.plan_hash,
            "split_hash": split.split_hash,
            "registry_hash": registry_hash,
            "source_inventory_hash": inventory_hash,
            "universe_hash": scan_plan.source_snapshot_content_hash,
        }
        if (any(getattr(execution_binding, key) != value
                for key, value in expected_binding.items())
                or set(execution_binding.source_catalog_hashes) != set(symbols)
                or checkpoint_dir is None or symbol_result_sink is None):
            raise CandleInputError("Stage A execution binding mismatch")
    checkpoint_binding = hashlib.sha256(canonical_json_bytes({
        "schema_version": "dev-source-symbol-scan/0.1.0",
        "algorithm_version": _ALGORITHM_VERSION,
        "scan_plan_hash": scan_plan.plan_hash,
        "split_hash": split.split_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "parameter_content_hash": parameter.content_hash,
        "registry_content_hash": registry_hash,
        "source_inventory_hash": inventory_hash,
        "source_validation_mode": "BOUND_RAW_AND_PARQUET_SHA256",
    })).hexdigest()
    if execution_binding is not None:
        checkpoint_binding = model_hash(execution_binding)
    totals: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    accepted: list[AcceptedP6Request] = []
    anomalies: list[str] = []
    scanned_symbols: list[str] = []
    validated_partitions = 0
    fallback_slots = 0
    fallback_price_plans = 0
    approximate_prices = 0
    approximate_outcomes = 0
    maximum_bps: Decimal | None = None
    left, right = parameter.candidate.parameters.pivot_window
    for position, symbol in enumerate(symbols, start=1):
        symbol_days = tuple(day for day in scan_plan.days if symbol in day.symbols)
        symbol_slot_count = sum(day.time_count for day in symbol_days)
        checkpoint_path = (
            _symbol_checkpoint_path(
                project_dir, checkpoint_dir, checkpoint_binding, symbol
            )
            if checkpoint_dir is not None
            else None
        )
        restored = (
            _read_symbol_checkpoint(
                checkpoint_path, binding_hash=checkpoint_binding, symbol=symbol
            )
            if checkpoint_path is not None
            else None
        )
        if restored is not None:
            if execution_binding is not None:
                validate_execution_checkpoint(restored, execution_binding)
                catalog = _load_symbol_sources(project_dir, symbol, final_period=final_period)
                if source_catalog_hash(catalog) != execution_binding.source_catalog_hashes[symbol]:
                    raise CandleInputError("checkpoint source-lineage mismatch")
                # Resuming validates current source bytes; old counters alone are not evidence.
                for interval in _INTERVALS:
                    for source in catalog[interval]:
                        _load_bound_candle_partition(project_dir, source)
                if symbol_result_sink is not None:
                    symbol_result_sink(restored)
            if (
                restored.get("symbol_slot_count") != symbol_slot_count
                or restored.get("source_partition_count")
                != partition_counts[symbol]
            ):
                raise CandleInputError(
                    "DEV source scan checkpoint source or slot count changed"
                )
            restored_accepted = [
                AcceptedP6Request.model_validate(item)
                for item in restored["accepted_requests"]
            ]
            if confirmed_trigger_sink is not None:
                for item in restored.get("confirmed_triggers", []):
                    confirmed_trigger_sink(ConfirmedTriggerRecord.model_validate(item))
            totals.update(restored["outcome_counts"])
            reasons.update(restored["reason_counts"])
            accepted.extend(restored_accepted)
            if restored["scanned"]:
                scanned_symbols.append(symbol)
            if restored["anomaly"] is not None:
                anomalies.append(str(restored["anomaly"]))
            validated_partitions += int(
                restored["validated_source_partition_count"]
            )
            fallback_slots += int(restored["fallback_rule_slot_count"])
            fallback_price_plans += int(restored["fallback_price_plan_count"])
            approximate_prices += int(
                restored["approximate_price_observation_count"]
            )
            approximate_outcomes += int(
                restored["approximate_outcome_warning_count"]
            )
            restored_maximum = restored["max_approximate_tick_adjustment_bps"]
            if restored_maximum is not None:
                maximum_bps = max(
                    maximum_bps or Decimal(0), Decimal(str(restored_maximum))
                )
            if progress is not None:
                progress(
                    position,
                    len(symbols),
                    symbol,
                    symbol_slot_count,
                    len(restored_accepted),
                )
            continue
        local: Counter[str] = Counter()
        local_reasons: Counter[str] = Counter()
        local_accepted: list[AcceptedP6Request] = []
        local_confirmed: list[ConfirmedTriggerRecord] = []
        local_fallback_slots = 0
        local_fallback_plans = 0
        local_approximate_prices = 0
        local_approximate_outcomes = 0
        local_maximum: Decimal | None = None
        local_validated_partitions = 0
        local_anomaly: str | None = None
        pivot_counts: dict[str, int] = {}
        zone_observation_counts: Counter[str] = Counter({"15m": 0, "1h": 0})
        structure_observation_slots = 0
        try:
            symbol_catalog = _load_symbol_sources(
                project_dir, symbol, final_period=final_period
            )
            if (execution_binding is not None and source_catalog_hash(symbol_catalog)
                    != execution_binding.source_catalog_hashes[symbol]):
                raise CandleInputError("source-lineage mismatch")
            symbol_rules = tuple(
                rule for rule in registry.entries if rule.symbol == symbol
            )
            if len(symbol_rules) != 1:
                raise CandleInputError(
                    "one lifecycle origin is required for each DEV scan symbol"
                )
            streams = {
                interval: _prepare_stream(
                    project_dir, symbol_catalog[interval],
                    end_exclusive=scan_plan.days[-1].end_exclusive,
                    lifecycle_start=symbol_rules[0].derived_first_candle_at,
                    pivot_window=(left, right),
                )
                for interval in _INTERVALS
            }
            local_validated_partitions = sum(
                item.source_count for item in streams.values()
            )
            pivot_counts = {
                "15m": sum(
                    scan_plan.days[0].first_confirmation
                    <= item.confirmed_at
                    < scan_plan.days[-1].end_exclusive
                    for item in streams["15m"].pivots
                ),
                "1h": sum(
                    scan_plan.days[0].first_confirmation
                    <= item.confirmed_at
                    < scan_plan.days[-1].end_exclusive
                    for item in streams["1h"].pivots
                ),
            }
            zone_states: dict[str, _IncrementalZoneState] = {
                "15m": _IncrementalZoneState(),
                "1h": _IncrementalZoneState(),
            }
            setup_cache: dict[tuple[int, int, int], SetupContextEvaluation | None] = {}
            active_rule = None
            for day in symbol_days:
                for index in range(day.time_count):
                    at = day.first_confirmation + timedelta(minutes=15 * index)
                    ends = {
                        interval: _visible_end(stream, at)
                        for interval, stream in streams.items()
                    }
                    if execution_binding is not None:
                        for interval, stream in streams.items():
                            end = ends[interval]
                            if ((end and stream.close_ns[end - 1] > pd.Timestamp(at).value)
                                    or (end < len(stream.close_ns)
                                        and stream.close_ns[end] <= pd.Timestamp(at).value)):
                                raise CandleInputError("point-in-time prefix violation")
                    if (
                        active_rule is None
                        or at < active_rule.effective_from
                        or (
                            active_rule.effective_to is not None
                            and at >= active_rule.effective_to
                        )
                    ):
                        active_rule = require_active_scan_rule(
                            registry=registry, symbol=symbol, at=at
                        )
                    rule = active_rule
                    if rule.verification_status.value == "UNVERIFIED":
                        local_fallback_slots += 1
                    setup_key = (ends["1h"], ends["4h"], ends["1d"])
                    if setup_key not in setup_cache:
                        try:
                            setup_cache[setup_key] = _setup_at(
                                streams, ends,
                                compression_threshold=float(
                                    parameter.candidate.parameters.compression_threshold
                                ),
                            )
                        except SetupNotReadyError as error:
                            setup_cache[setup_key] = None
                            local_reasons[f"SETUP_NOT_READY:{error}"] += 1
                    setup = setup_cache[setup_key]
                    if setup is None:
                        local["setup_not_ready_count"] += 1
                        continue
                    local["setup_evaluated_count"] += 1
                    if not setup.eligible_for_trigger:
                        local_reasons[f"SETUP:{setup.decision_reason.value}"] += 1
                        continue
                    local["setup_eligible_count"] += 1
                    fifteen, fifteen_indicators = _slice(
                        streams["15m"], ends["15m"], 97
                    )
                    hourly, hourly_indicators = _slice(
                        streams["1h"], ends["1h"], 60
                    )
                    hourly_setup = setup.one_hour
                    if (
                        hourly_setup is None
                        or hourly_setup.episode_id is None
                        or hourly_setup.episode_start is None
                    ):
                        raise CandleInputError(
                            "eligible batch setup lacks a pullback episode"
                        )
                    fifteen_pivots, fifteen_zones = _available_structure(
                        streams["15m"],
                        at,
                        zone_states["15m"],
                        pivot_since=hourly_setup.episode_start,
                        zone_since=pd.Timestamp(
                            fifteen["open_time"].iloc[0]
                        ).to_pydatetime(),
                    )
                    hourly_pivots, hourly_zones = _available_structure(
                        streams["1h"],
                        at,
                        zone_states["1h"],
                        pivot_since=hourly_setup.episode_start,
                        zone_since=pd.Timestamp(
                            hourly["open_time"].iloc[0]
                        ).to_pydatetime(),
                    )
                    structure_observation_slots += 1
                    zone_observation_counts["15m"] += len(fifteen_zones)
                    zone_observation_counts["1h"] += len(hourly_zones)
                    if execution_binding is not None and (
                        any(item.confirmed_at > at for item in fifteen_pivots)
                        or any(item.confirmed_at > at for item in fifteen_zones)
                        or any(item.confirmed_at > at for item in hourly_pivots)
                        or any(item.confirmed_at > at for item in hourly_zones)
                    ):
                        raise CandleInputError("point-in-time structure violation")
                    columns = (
                        ("sma30", "sma60")
                        if hourly_setup.state is PullbackState.SHALLOW
                        else ("sma60", "sma90")
                    )
                    bounds = sorted(
                        float(hourly_indicators[column].iloc[-1])
                        for column in columns
                    )
                    trigger_context = TriggerSetupContext(
                        symbol=symbol,
                        direction=setup.four_hour.direction,
                        pullback_state=hourly_setup.state,
                        eligible_for_trigger=True,
                        episode_id=hourly_setup.episode_id,
                        episode_start=hourly_setup.episode_start,
                        region_lower=bounds[0],
                        region_upper=bounds[1],
                    )
                    try:
                        decision = evaluate_triggers(
                            fifteen,
                            fifteen_indicators,
                            setup=trigger_context,
                            pivots=fifteen_pivots,
                            zones=fifteen_zones,
                        )
                    except TriggerNotReadyError as error:
                        local["trigger_not_ready_count"] += 1
                        local_reasons[f"TRIGGER_NOT_READY:{error}"] += 1
                        continue
                    local["trigger_evaluated_count"] += 1
                    if not decision.eligible_for_plan:
                        local_reasons.update(
                            f"TRIGGER:{item.value}"
                            for item in decision.trigger_a.reason_codes
                        )
                        continue
                    local["trigger_confirmed_count"] += 1
                    request = EntryStopRequest.from_trigger_decision(
                        decision,
                        confirmation_high=fifteen["high"].iloc[-1],
                        confirmation_low=fifteen["low"].iloc[-1],
                        atr_at_confirmation=float(
                            fifteen_indicators["atr14"].iloc[-1]
                        ),
                        tick_size=rule.tick_size,
                    )
                    params = parameter.candidate.parameters
                    entry_stop = build_entry_stop(
                        request,
                        entry_ttl_bars=params.entry_ttl_bars,
                        stop_atr_multiplier=params.stop_atr_multiplier,
                    )
                    take_profit = None
                    if entry_stop.accepted:
                        take_profit = build_take_profit(
                            entry_stop,
                            fifteen_minute_candles=fifteen.iloc[-96:],
                            one_hour_candles=hourly,
                            fifteen_minute_zones=fifteen_zones,
                            one_hour_zones=hourly_zones,
                        )
                    data_request = None
                    impact = _approximate_tick_impact(
                        rule, entry_stop, take_profit
                    )
                    if impact is not None:
                        local_fallback_plans += 1
                        local_approximate_prices += len(impact.price_adjustments)
                        local_approximate_outcomes += int(impact.outcome_warning)
                        if impact.max_adjustment_bps is not None:
                            local_maximum = max(
                                local_maximum or Decimal(0),
                                impact.max_adjustment_bps,
                            )
                    if not entry_stop.accepted:
                        local["entry_stop_rejected_count"] += 1
                        local_reasons.update(
                            f"ENTRY_STOP:{item.value}"
                            for item in entry_stop.reason_codes
                        )
                        p6_status = "REJECTED_ENTRY_STOP"
                    elif take_profit is None or not take_profit.accepted:
                        local["take_profit_rejected_count"] += 1
                        if take_profit is not None:
                            local_reasons.update(
                                f"TAKE_PROFIT:{item.value}"
                                for item in take_profit.reason_codes
                            )
                        p6_status = "REJECTED_TAKE_PROFIT"
                    else:
                        try:
                            data_request = build_dev_replay_data_request(
                                trade_plan=take_profit,
                                plan=plan,
                                parameter=parameter,
                                split=split,
                                universe=by_date[day.selection_date],
                                registry=registry,
                            )
                        except ReplayDataInputError as error:
                            local["request_boundary_rejected_count"] += 1
                            local_reasons[f"REQUEST_REJECTED:{error}"] += 1
                            p6_status = "REJECTED_REQUEST_BOUNDARY"
                        else:
                            local["accepted_count"] += 1
                            local_accepted.append(AcceptedP6Request(
                                request=data_request, approximate_tick_impact=impact,
                            ))
                            p6_status = "ACCEPTED_PLAN"
                    primary = decision.primary_trigger
                    if primary is None:
                        raise CandleInputError("confirmed trigger lacks a primary trigger")
                    if primary is TriggerType.MA_RECLAIM:
                        trigger_a_evidence = decision.trigger_a.evidence
                        structure_context = {
                            "trigger_type": primary.value,
                            "structure_pivot_id": trigger_a_evidence.structure_pivot_id,
                            "structure_price": trigger_a_evidence.structure_price,
                            "invalidation_price": decision.trigger_a.invalidation_price,
                        }
                    else:
                        if decision.trigger_b is None:
                            raise CandleInputError("confirmed sweep trigger lacks Trigger B")
                        trigger_b_evidence = decision.trigger_b.evidence
                        structure_context = {
                            "trigger_type": primary.value,
                            "selected_zone_id": trigger_b_evidence.selected_zone_id,
                            "zone_lower": trigger_b_evidence.zone_lower,
                            "zone_upper": trigger_b_evidence.zone_upper,
                            "sweep_extreme": trigger_b_evidence.sweep_extreme,
                            "invalidation_price": decision.trigger_b.invalidation_price,
                        }
                    source_partitions = tuple(
                        ConfirmedTriggerSourcePartition(
                            interval=interval,
                            period=source.spec.period,
                            source_file_hash=source.download.actual_sha256,
                            normalized_content_hash=source.normalization.normalized_content_hash,
                            parquet_sha256=source.normalization.parquet_sha256,
                        )
                        for interval in _INTERVALS
                        for source in symbol_catalog[interval]
                    )
                    record_payload = {
                        "schema_version": "confirmed-trigger-record/0.1.0",
                        "symbol": symbol,
                        "logical_signal_id": decision.logical_signal_id,
                        "direction": decision.direction,
                        "primary_trigger": primary,
                        "confirmation_open_time": decision.confirmation_open_time,
                        "confirmation_close_time": decision.confirmation_close_time,
                        "confirmation_close_price": Decimal(str(fifteen["close"].iloc[-1])),
                        "trigger_decision": decision,
                        "setup_context": setup,
                        "visible_pivots": fifteen_pivots,
                        "visible_zones": fifteen_zones,
                        "visible_hourly_pivots": hourly_pivots,
                        "visible_hourly_zones": hourly_zones,
                        "atr_at_confirmation": Decimal(str(fifteen_indicators["atr14"].iloc[-1])),
                        "atr_timestamp": pd.Timestamp(
                            fifteen["close_time_exclusive"].iloc[-1]
                        ).to_pydatetime(),
                        "invalidation_price": request.invalidation_price,
                        "structure_context": structure_context,
                        "tick_size": rule.tick_size,
                        "tick_size_rule_content_hash": model_hash(rule),
                        "tick_size_verification_status": rule.verification_status.value,
                        "tick_size_confidence": rule.confidence.value,
                        "tick_size_warning_codes": (
                            (APPROXIMATE_TICK_SIZE_WARNING,)
                            if rule.verification_status.value == "UNVERIFIED" else ()
                        ),
                        "approximate_tick_impact": (
                            impact
                        ),
                        "entry_stop_request": request,
                        "baseline_entry_stop": entry_stop,
                        "baseline_take_profit": take_profit,
                        "baseline_data_request": (
                            data_request if p6_status == "ACCEPTED_PLAN" else None
                        ),
                        "baseline_p6_status": p6_status,
                        "baseline_rejection_reasons": (
                            tuple(item.value for item in entry_stop.reason_codes)
                            if p6_status == "REJECTED_ENTRY_STOP"
                            else tuple(item.value for item in take_profit.reason_codes)
                            if p6_status == "REJECTED_TAKE_PROFIT" and take_profit is not None
                            else ()
                        ),
                        "universe_content_hash": model_hash(by_date[day.selection_date]),
                        "source_inventory_hash": inventory_hash,
                        "source_partitions": source_partitions,
                        "scan_plan_hash": scan_plan.plan_hash,
                        "split_hash": split.split_hash,
                        "sensitivity_plan_hash": plan.plan_hash,
                        "parameter_content_hash": parameter.content_hash,
                        "registry_content_hash": registry_hash,
                        "algorithm_version": _ALGORITHM_VERSION,
                        "strategy_version": parameter.strategy_version,
                        "trigger_version": decision.version,
                        "setup_version": setup.version,
                        "trade_plan_version": entry_stop.version,
                        "pivot_version": PIVOT_VERSION,
                        "zone_version": PIVOT_ZONE_VERSION,
                    }
                    record_seed = ConfirmedTriggerRecord.model_construct(
                        **record_payload, record_hash="0" * 64
                    )
                    normalized_record_payload = record_seed.model_dump(
                        mode="json", exclude={"record_hash"}
                    )
                    record = ConfirmedTriggerRecord.model_validate({
                        **normalized_record_payload,
                        "record_hash": hashlib.sha256(
                            canonical_json_bytes(normalized_record_payload)
                        ).hexdigest(),
                    })
                    local_confirmed.append(record)
                    if confirmed_trigger_sink is not None:
                        confirmed_trigger_sink(record)
            scanned_symbols.append(symbol)
        except (CandleInputError, OSError, ValueError) as error:
            if execution_binding is not None:
                raise CandleInputError(f"Stage A stopped at {symbol}: {error}") from error
            local_anomaly = f"{symbol}:{type(error).__name__}:{error}"
            anomalies.append(local_anomaly)
            totals["source_error_slot_count"] += symbol_slot_count
        else:
            validated_partitions += local_validated_partitions
            totals.update(local)
            reasons.update(local_reasons)
            accepted.extend(local_accepted)
            fallback_slots += local_fallback_slots
            fallback_price_plans += local_fallback_plans
            approximate_prices += local_approximate_prices
            approximate_outcomes += local_approximate_outcomes
            if local_maximum is not None:
                maximum_bps = max(maximum_bps or Decimal(0), local_maximum)
        if checkpoint_path is not None:
            checkpoint_payload = {
                "schema_version": "dev-source-symbol-scan/0.1.0",
                "binding_hash": checkpoint_binding,
                "symbol": symbol,
                "symbol_slot_count": symbol_slot_count,
                "outcome_counts": (
                    {"source_error_slot_count": symbol_slot_count}
                    if local_anomaly is not None
                    else dict(sorted(local.items()))
                ),
                "reason_counts": (
                    {} if local_anomaly is not None
                    else dict(sorted(local_reasons.items()))
                ),
                "accepted_requests": [
                    item.model_dump(mode="json") for item in local_accepted
                ] if local_anomaly is None else [],
                "confirmed_triggers": [
                    item.model_dump(mode="json") for item in local_confirmed
                ] if local_anomaly is None else [],
                "scanned": local_anomaly is None,
                "anomaly": local_anomaly,
                "validated_source_partition_count": (
                    local_validated_partitions if local_anomaly is None else 0
                ),
                "source_partition_count": partition_counts[symbol],
                "fallback_rule_slot_count": (
                    local_fallback_slots if local_anomaly is None else 0
                ),
                "fallback_price_plan_count": (
                    local_fallback_plans if local_anomaly is None else 0
                ),
                "approximate_price_observation_count": (
                    local_approximate_prices if local_anomaly is None else 0
                ),
                "approximate_outcome_warning_count": (
                    local_approximate_outcomes if local_anomaly is None else 0
                ),
                "max_approximate_tick_adjustment_bps": (
                    str(local_maximum)
                    if local_anomaly is None and local_maximum is not None
                    else None
                ),
            }
            if execution_binding is not None:
                local_funnel = {
                    name: local.get(name, 0) for name in DevScanFunnel.model_fields
                }
                local_funnel["scan_slot_count"] = symbol_slot_count
                checkpoint_payload.update({
                    "execution_binding": execution_binding.model_dump(mode="json"),
                    "diagnostics": {
                        "funnel": local_funnel,
                        "pivot_counts": pivot_counts,
                        "zone_observation_counts": dict(zone_observation_counts),
                        "structure_observation_slots": structure_observation_slots,
                    },
                })
                validate_execution_checkpoint(checkpoint_payload, execution_binding)
                if symbol_result_sink is not None:
                    symbol_result_sink(checkpoint_payload)
            _write_symbol_checkpoint(checkpoint_path, checkpoint_payload)
        if progress is not None:
            progress(
                position,
                len(symbols),
                symbol,
                symbol_slot_count,
                len(local_accepted),
            )
    accepted.sort(key=lambda item: (item.request.start, item.request.request.armed.symbol))
    accepted_tuple = tuple(accepted)
    totals["scan_slot_count"] = scan_plan.expected_record_count
    funnel = DevScanFunnel.model_validate(
        {field: totals[field] for field in DevScanFunnel.model_fields}
    )
    merged_count, merged_minutes = _merged_windows(accepted_tuple)
    accepted_symbols = tuple(sorted({item.request.request.armed.symbol for item in accepted_tuple}))
    requirements = DevMarketDataRequirement(
        symbols=accepted_symbols,
        one_minute_rows_before_overlap_dedup=sum(
            item.request.expected_candle_count for item in accepted_tuple
        ),
        one_minute_rows_after_overlap_dedup=merged_minutes,
        funding_windows_before_overlap_dedup=len(accepted_tuple),
        funding_windows_after_overlap_dedup=merged_count,
        merged_window_minutes=merged_minutes,
    )
    request_payload = [item.model_dump(mode="json") for item in accepted_tuple]
    request_document = {"accepted_requests": request_payload}
    payload: dict[str, Any] = {
        "schema_version": "dev-source-scan/0.1.0",
        "scan_plan_hash": scan_plan.plan_hash,
        "split_hash": split.split_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "parameter_content_hash": parameter.content_hash,
        "registry_content_hash": registry_hash,
        "source_inventory_hash": inventory_hash,
        "source_validation_mode": "BOUND_RAW_AND_PARQUET_SHA256",
        "source_partition_count": partition_count,
        "validated_source_partition_count": validated_partitions,
        "scan_start": scan_plan.days[0].first_confirmation.isoformat().replace(
            "+00:00", "Z"
        ),
        "scan_end_exclusive": scan_plan.days[-1].end_exclusive.isoformat().replace(
            "+00:00", "Z"
        ),
        "universe_symbol_count": len(symbols),
        "scanned_symbols": sorted(scanned_symbols),
        "accepted_symbols": list(accepted_symbols),
        "funnel": funnel.model_dump(mode="json"),
        "reason_counts": dict(sorted(reasons.items())),
        "fallback_rule_slot_count": fallback_slots,
        "fallback_price_plan_count": fallback_price_plans,
        "approximate_price_observation_count": approximate_prices,
        "approximate_outcome_warning_count": approximate_outcomes,
        "max_approximate_tick_adjustment_bps": (
            str(maximum_bps) if maximum_bps is not None else None
        ),
        "warning_codes": (
            [APPROXIMATE_TICK_SIZE_WARNING] if fallback_slots else []
        ),
        "accepted_requests": request_payload,
        "request_set_hash": hashlib.sha256(
            canonical_json_bytes(request_document)
        ).hexdigest(),
        "market_data_requirement": requirements.model_dump(mode="json"),
        "anomalies": sorted(anomalies),
        "status": "PARTIAL" if anomalies else "COMPLETE",
        "download_authorized": False,
        "replay_executed": False,
        "locked_test_consumed": False,
    }
    return DevSourceScanReport.model_validate({
        **payload,
        "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def write_dev_source_scan_report(report: DevSourceScanReport, data_dir: Path) -> Path:
    report = DevSourceScanReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests/dev_source_scan" / f"{report.report_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise CandleInputError("DEV source scan report path escapes data directory")
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise CandleInputError("existing DEV source scan report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination


def write_confirmed_trigger_ledger(
    ledger: ConfirmedTriggerLedger, data_dir: Path,
) -> Path:
    """Write the independent immutable confirmed-trigger ledger namespace."""

    ledger = ConfirmedTriggerLedger.model_validate(ledger.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "confirmed_trigger_ledger" / f"{ledger.ledger_hash}.json"
    )
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise CandleInputError("confirmed trigger ledger path escapes data directory")
    content = canonical_json_bytes(ledger.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise CandleInputError("existing confirmed trigger ledger changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
