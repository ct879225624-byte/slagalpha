"""Confirmed pivot events and deterministic same-type structure zones."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from typing import Self, cast

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.strategy.indicators import IndicatorInputError, validate_indicator_input

PIVOT_VERSION = "confirmed-pivot/0.1.0"
PIVOT_ZONE_VERSION = "pivot-zone/0.1.0"
ZONE_ATR_MULTIPLIER = 0.2
ALLOWED_PIVOT_WINDOWS = frozenset({(2, 2), (3, 3)})


class PivotType(StrEnum):
    """Price structure direction."""

    HIGH = "HIGH"
    LOW = "LOW"


class PivotEvent(BaseModel):
    """A pivot that only becomes visible when its right window has closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = PIVOT_VERSION
    pivot_id: str
    interval: str
    kind: PivotType
    pivot_time: datetime
    confirmed_at: datetime
    center_position: int = Field(ge=0)
    confirmed_position: int = Field(ge=0)
    price: float
    atr_at_confirmation: float = Field(ge=0)
    left: int = Field(ge=1)
    right: int = Field(ge=1)

    @field_validator("pivot_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("pivot_id must be a lowercase SHA-256 value")
        return value

    @field_validator("pivot_time", "confirmed_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("pivot timestamps must be timezone-aware")
        return value

    @field_validator("price", "atr_at_confirmation")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("pivot numeric values must be finite")
        return value

    @model_validator(mode="after")
    def validate_event_order(self) -> Self:
        if self.confirmed_at <= self.pivot_time:
            raise ValueError("confirmed_at must be later than pivot_time")
        if self.confirmed_position <= self.center_position:
            raise ValueError("confirmed_position must be later than center_position")
        if (self.left, self.right) not in ALLOWED_PIVOT_WINDOWS:
            raise ValueError("only symmetric 2/2 or 3/3 pivots are allowed")
        return self


class PivotZone(BaseModel):
    """One same-type sequence of adjacent pivots merged by the frozen ATR rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = PIVOT_ZONE_VERSION
    zone_id: str
    interval: str
    kind: PivotType
    lower: float
    upper: float
    confirmed_at: datetime
    members: tuple[PivotEvent, ...]

    @field_validator("zone_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("zone_id must be a lowercase SHA-256 value")
        return value

    @model_validator(mode="after")
    def validate_zone(self) -> Self:
        if not self.members:
            raise ValueError("zone must contain at least one pivot")
        if not math.isfinite(self.lower) or not math.isfinite(self.upper):
            raise ValueError("zone bounds must be finite")
        if self.lower > self.upper:
            raise ValueError("zone lower must not exceed upper")
        if any(member.interval != self.interval for member in self.members):
            raise ValueError("zone members must use one interval")
        if any(member.kind is not self.kind for member in self.members):
            raise ValueError("zone members must use one pivot type")
        if self.confirmed_at != self.members[-1].confirmed_at:
            raise ValueError("zone confirmed_at must match its last member")
        return self

    @property
    def member_pivot_ids(self) -> tuple[str, ...]:
        return tuple(member.pivot_id for member in self.members)


def _canonical_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _pivot_id(
    interval: str,
    kind: PivotType,
    pivot_time: datetime,
    confirmed_at: datetime,
    price: float,
    left: int,
    right: int,
) -> str:
    canonical = "|".join(
        (
            PIVOT_VERSION,
            interval,
            kind.value,
            _canonical_timestamp(pivot_time),
            _canonical_timestamp(confirmed_at),
            format(price, ".17g"),
            str(left),
            str(right),
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _timestamp_at(values: pd.Series, position: int, name: str) -> datetime:
    timestamp = pd.Timestamp(values.iloc[position])
    if timestamp.tzinfo is None:
        raise IndicatorInputError(f"{name} must be timezone-aware")
    return cast(datetime, timestamp.to_pydatetime())


def detect_confirmed_pivots(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    interval: str,
    *,
    left: int = 2,
    right: int = 2,
) -> tuple[PivotEvent, ...]:
    """Emit pivots only at positions whose complete right window has closed."""

    if (left, right) not in ALLOWED_PIVOT_WINDOWS:
        raise ValueError("only symmetric 2/2 or 3/3 pivots are allowed")
    validate_indicator_input(candles)
    if "close_time_exclusive" not in candles:
        raise IndicatorInputError("missing required column: close_time_exclusive")
    if "atr14" not in indicators:
        raise IndicatorInputError("missing required indicator: atr14")
    if len(candles) != len(indicators):
        raise IndicatorInputError("candles and indicators lengths must match")
    if not candles.index.equals(indicators.index):
        raise IndicatorInputError("candles and indicators indexes must match")

    close_times = pd.DatetimeIndex(candles["close_time_exclusive"])
    if close_times.tz is None:
        raise IndicatorInputError("close_time_exclusive must be timezone-aware")
    if not close_times.is_monotonic_increasing or close_times.has_duplicates:
        raise IndicatorInputError(
            "close_time_exclusive must be unique and strictly increasing"
        )

    high = np.asarray(candles["high"], dtype=np.float64)
    low = np.asarray(candles["low"], dtype=np.float64)
    atr = np.asarray(indicators["atr14"], dtype=np.float64)
    events: list[PivotEvent] = []

    for center in range(left, len(candles) - right):
        confirmed_position = center + right
        atr_at_confirmation = float(atr[confirmed_position])
        if not math.isfinite(atr_at_confirmation):
            continue

        pivot_time = _timestamp_at(candles["open_time"], center, "open_time")
        confirmed_at = _timestamp_at(
            candles["close_time_exclusive"],
            confirmed_position,
            "close_time_exclusive",
        )
        left_lows = low[center - left : center]
        right_lows = low[center + 1 : center + right + 1]
        left_highs = high[center - left : center]
        right_highs = high[center + 1 : center + right + 1]

        candidates: list[tuple[PivotType, float]] = []
        center_low = float(low[center])
        if bool((center_low < left_lows).all()) and bool(
            (center_low <= right_lows).all()
        ):
            candidates.append((PivotType.LOW, center_low))

        center_high = float(high[center])
        if bool((center_high > left_highs).all()) and bool(
            (center_high >= right_highs).all()
        ):
            candidates.append((PivotType.HIGH, center_high))

        for kind, price in candidates:
            events.append(
                PivotEvent(
                    pivot_id=_pivot_id(
                        interval,
                        kind,
                        pivot_time,
                        confirmed_at,
                        price,
                        left,
                        right,
                    ),
                    interval=interval,
                    kind=kind,
                    pivot_time=pivot_time,
                    confirmed_at=confirmed_at,
                    center_position=center,
                    confirmed_position=confirmed_position,
                    price=price,
                    atr_at_confirmation=atr_at_confirmation,
                    left=left,
                    right=right,
                )
            )

    return tuple(
        sorted(
            events,
            key=lambda event: (
                event.confirmed_at,
                event.pivot_time,
                event.kind.value,
                event.pivot_id,
            ),
        )
    )


def _zone_id(interval: str, kind: PivotType, members: list[PivotEvent]) -> str:
    canonical = "|".join(
        (PIVOT_ZONE_VERSION, interval, kind.value, *(member.pivot_id for member in members))
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _build_zone(members: list[PivotEvent]) -> PivotZone:
    first = members[0]
    prices = [member.price for member in members]
    return PivotZone(
        zone_id=_zone_id(first.interval, first.kind, members),
        interval=first.interval,
        kind=first.kind,
        lower=min(prices),
        upper=max(prices),
        confirmed_at=members[-1].confirmed_at,
        members=tuple(members),
    )


def merge_pivot_zones(pivots: tuple[PivotEvent, ...]) -> tuple[PivotZone, ...]:
    """Merge adjacent same-type pivots using the later pivot's confirmation ATR."""

    grouped: dict[tuple[str, PivotType], list[PivotEvent]] = defaultdict(list)
    for pivot in pivots:
        grouped[(pivot.interval, pivot.kind)].append(pivot)

    zones: list[PivotZone] = []
    for group_key in sorted(grouped, key=lambda item: (item[0], item[1].value)):
        ordered = sorted(
            grouped[group_key],
            key=lambda pivot: (pivot.confirmed_at, pivot.pivot_time, pivot.pivot_id),
        )
        current_members: list[PivotEvent] = []
        for pivot in ordered:
            if not current_members:
                current_members = [pivot]
                continue

            previous = current_members[-1]
            threshold = ZONE_ATR_MULTIPLIER * pivot.atr_at_confirmation
            if abs(pivot.price - previous.price) < threshold:
                current_members.append(pivot)
            else:
                zones.append(_build_zone(current_members))
                current_members = [pivot]
        if current_members:
            zones.append(_build_zone(current_members))

    return tuple(
        sorted(
            zones,
            key=lambda zone: (
                zone.confirmed_at,
                zone.interval,
                zone.kind.value,
                zone.zone_id,
            ),
        )
    )
