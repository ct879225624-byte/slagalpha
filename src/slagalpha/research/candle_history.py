"""Continuous closed history tied to an exact DEV scan slot; no implicit ATR seed approval."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from slagalpha.data.klines import INTERVAL_MILLISECONDS, _normalized_content_hash
from slagalpha.domain.symbols import normalize_symbol
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_inputs import (
    CandleInputError,
    CandlePartitionSource,
    load_verified_candle_partition,
)
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.scan_plan import DevScanPlan, model_hash

ScanInterval = Literal["15m", "1h", "4h", "1d"]


def last_closed_boundary(confirmation: datetime, interval: ScanInterval) -> datetime:
    seconds = INTERVAL_MILLISECONDS[interval] // 1000
    return datetime.fromtimestamp(int(confirmation.timestamp()) // seconds * seconds, UTC)


def _required_months(start: datetime, end: datetime) -> tuple[str, ...]:
    current = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = []
    while current < end:
        months.append(current.strftime("%Y-%m"))
        current = (current.replace(year=current.year + 1, month=1) if current.month == 12
                   else current.replace(month=current.month + 1))
    return tuple(months)


class ScanCandleHistory(BaseModel):
    """A reproducible input prefix, not proof that the declared starting seed is sufficient."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["scan-candle-history/0.1.0"] = "scan-candle-history/0.1.0"
    scan_plan_hash: Sha256
    universe_content_hash: Sha256
    symbol: str
    interval: ScanInterval
    history_start: datetime
    confirmation_close: datetime
    last_close_exclusive: datetime
    row_count: int = Field(gt=0, strict=True)
    sources: tuple[CandlePartitionSource, ...] = Field(min_length=1)
    normalized_content_hash: Sha256
    history_seed_verified: Literal[False] = False
    research_authorized: Literal[False] = False
    content_hash: Sha256

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if value != normalize_symbol(value):
            raise ValueError("history symbol must be canonical")
        return value

    @field_validator("history_start", "confirmation_close", "last_close_exclusive")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond:
            raise ValueError("history times must use exact UTC seconds")
        return value

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        interval_seconds = INTERVAL_MILLISECONDS[self.interval] // 1000
        if self.confirmation_close.minute % 15 or self.confirmation_close.second:
            raise ValueError("history confirmation must align to a 15m scan slot")
        if int(self.history_start.timestamp()) % interval_seconds:
            raise ValueError("history start must align to the interval grid")
        expected_close = last_closed_boundary(self.confirmation_close, self.interval)
        if self.last_close_exclusive != expected_close:
            raise ValueError("history must end at the latest fully closed interval")
        seconds = int((self.last_close_exclusive - self.history_start).total_seconds())
        count = seconds // interval_seconds
        if self.row_count != count:
            raise ValueError("history count must cover the complete declared prefix")
        if tuple(item.spec.period for item in self.sources) != _required_months(
            self.history_start, self.last_close_exclusive,
        ):
            raise ValueError("history sources must exactly cover canonical months")
        if any((item.spec.symbol, item.spec.interval) != (self.symbol, self.interval)
               for item in self.sources):
            raise ValueError("history sources must use one exact symbol and interval")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("history content hash mismatch")
        return self


class ScanHistoryLineage:
    """Keep one declared origin and frozen monthly receipts per stream within a scan plan.

    This in-memory consistency check neither approves the origin nor caches raw verification.
    New months can extend a prefix; an already observed month or origin cannot be replaced.
    """

    def __init__(self, scan_plan_hash: str) -> None:
        self.scan_plan_hash = TypeAdapter(Sha256).validate_python(scan_plan_hash)
        self._origins: dict[tuple[str, ScanInterval], datetime] = {}
        self._partitions: dict[tuple[str, ScanInterval, str], str] = {}

    def require(self, history: ScanCandleHistory) -> None:
        history = ScanCandleHistory.model_validate(history.model_dump(mode="json"))
        if history.scan_plan_hash != self.scan_plan_hash:
            raise CandleInputError("history lineage belongs to a different scan plan")
        stream = (history.symbol, history.interval)
        if self._origins.get(stream, history.history_start) != history.history_start:
            raise CandleInputError("scan history origin changed within the same input lineage")
        partitions = {(history.symbol, history.interval, source.spec.period): model_hash(source)
                      for source in history.sources}
        if any(key in self._partitions and self._partitions[key] != digest
               for key, digest in partitions.items()):
            raise CandleInputError("scan history partition receipt changed within the same lineage")
        # Commit only after every comparison passes; errors do not silently reset the origin.
        self._origins[stream] = history.history_start
        self._partitions.update(partitions)


def load_scan_candle_history(
    *, project_dir: Path, scan_plan: DevScanPlan, symbol: str, interval: ScanInterval,
    confirmation_close: datetime, history_start: datetime,
    sources: tuple[CandlePartitionSource, ...],
) -> tuple[pd.DataFrame, ScanCandleHistory]:
    """Verify full source partitions, then expose only the complete closed prefix at a scan slot."""
    scan_plan = DevScanPlan.model_validate(scan_plan.model_dump(mode="json"))
    # Validate geometry/identities before IO through the same model as the final receipt.
    if any(at.tzinfo is None or at.utcoffset() != timedelta(0)
           for at in (confirmation_close, history_start)):
        raise CandleInputError("scan history boundaries must use UTC")
    day = next((item for item in scan_plan.days
                if item.first_confirmation <= confirmation_close < item.end_exclusive), None)
    if day is None or symbol not in day.symbols:
        raise CandleInputError("history request does not belong to a DEV Universe scan slot")
    if interval not in ("15m", "1h", "4h", "1d"):
        raise CandleInputError("unsupported scan history interval")
    last_close = last_closed_boundary(confirmation_close, interval)
    seconds = INTERVAL_MILLISECONDS[interval] // 1000
    payload = {
        "schema_version": "scan-candle-history/0.1.0", "scan_plan_hash": scan_plan.plan_hash,
        "universe_content_hash": day.universe_content_hash, "symbol": symbol, "interval": interval,
        "history_start": history_start.isoformat().replace("+00:00", "Z"),
        "confirmation_close": confirmation_close.isoformat().replace("+00:00", "Z"),
        "last_close_exclusive": last_close.isoformat().replace("+00:00", "Z"),
        "row_count": int((last_close - history_start).total_seconds()) // seconds,
        "sources": [source.model_dump(mode="json") for source in sources],
        "normalized_content_hash": "0" * 64,
        "history_seed_verified": False, "research_authorized": False,
    }
    scope = ScanCandleHistory.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    chunks = []
    for source in scope.sources:
        frame = load_verified_candle_partition(project_dir=project_dir, source=source)
        chunks.append(frame.loc[(frame["open_time"] >= history_start)
                                & (frame["close_time_exclusive"] <= last_close)])
    visible = pd.concat(chunks, ignore_index=True)
    expected_opens = pd.date_range(
        start=history_start, periods=scope.row_count, freq=pd.Timedelta(seconds=seconds),
    )
    observed_opens = pd.DatetimeIndex(visible["open_time"]).as_unit("ms")
    if (len(visible) != scope.row_count
        or not observed_opens.equals(expected_opens.as_unit("ms"))):
        raise CandleInputError("Candle history has a missing, duplicate or out-of-order closed bar")
    payload["normalized_content_hash"] = _normalized_content_hash(visible)
    result = ScanCandleHistory.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    return visible, result


def reload_scan_candle_history(
    *, project_dir: Path, scan_plan: DevScanPlan, history: ScanCandleHistory,
) -> pd.DataFrame:
    history = ScanCandleHistory.model_validate(history.model_dump(mode="json"))
    if history.scan_plan_hash != scan_plan.plan_hash:
        raise CandleInputError("saved history no longer matches the selected scan plan")
    visible, observed = load_scan_candle_history(
        project_dir=project_dir, scan_plan=scan_plan,
        symbol=history.symbol, interval=history.interval,
        confirmation_close=history.confirmation_close, history_start=history.history_start,
        sources=history.sources,
    )
    if observed != history:
        raise CandleInputError("saved history no longer matches scan plan or source content")
    return visible
