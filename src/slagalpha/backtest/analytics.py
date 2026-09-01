"""Funding protection and trade-level MAE/MFE for P7.4b."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Self, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from slagalpha.backtest.costs import CostedReplayResult, CostScenario
from slagalpha.backtest.replay import (
    REPLAY_VERSION,
    ArmedState,
    PositionState,
    ReplayEventType,
    TradeExitReason,
    validate_replay_candles,
)
from slagalpha.strategy.setup import Direction


class FundingObservation(BaseModel):
    """One expected settlement with nullable exchange data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    settlement_time: datetime
    rate: Decimal | None
    mark_price: Decimal | None

    @field_validator("settlement_time")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Funding settlement_time must use UTC")
        return value

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if self.rate is not None and not self.rate.is_finite():
            raise ValueError("Funding rate must be finite when present")
        if self.mark_price is not None and (
            not self.mark_price.is_finite() or self.mark_price <= 0
        ):
            raise ValueError("Funding mark_price must be finite and positive when present")
        return self


class FundingDataset(BaseModel):
    """Versionable schedule coverage plus observations; no schedule is hardcoded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_start: datetime
    coverage_end: datetime
    expected_settlement_times: tuple[datetime, ...]
    observations: tuple[FundingObservation, ...]

    @field_validator("coverage_start", "coverage_end")
    @classmethod
    def validate_coverage_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Funding coverage timestamps must use UTC")
        return value

    @field_validator("expected_settlement_times")
    @classmethod
    def validate_expected_times(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        if any(value.tzinfo is None or value.utcoffset() != timedelta(0) for value in values):
            raise ValueError("expected Funding times must use UTC")
        if tuple(sorted(values)) != values or len(set(values)) != len(values):
            raise ValueError("expected Funding times must be unique and sorted")
        return values

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if self.coverage_end <= self.coverage_start:
            raise ValueError("Funding coverage_end must be after coverage_start")
        if any(
            time < self.coverage_start or time >= self.coverage_end
            for time in self.expected_settlement_times
        ):
            raise ValueError("expected Funding time is outside dataset coverage")
        observation_times = tuple(item.settlement_time for item in self.observations)
        if tuple(sorted(observation_times)) != observation_times or len(
            set(observation_times)
        ) != len(observation_times):
            raise ValueError("Funding observations must be unique and sorted")
        if any(time not in self.expected_settlement_times for time in observation_times):
            raise ValueError("Funding observation is not in the expected schedule")
        return self


class FundingApplication(BaseModel):
    """Auditable applied or missing Funding event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: ReplayEventType
    settlement_time: datetime
    rate: Decimal | None
    mark_price: Decimal | None
    open_fraction: Decimal
    cash_flow: Decimal | None


class FundingAdjustedResult(BaseModel):
    """Closed-trade Net result after Funding completeness checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    costed: CostedReplayResult
    applications: tuple[FundingApplication, ...]
    funding_cash_flow: Decimal
    funding_data_missing: bool
    missing_settlement_times: tuple[datetime, ...]
    net_statistics_eligible: bool
    net_pnl: Decimal | None
    net_r: Decimal | None


class ExcursionResult(BaseModel):
    """Trade-level OHLC fallback MAE/MFE without resetting after TP1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ordering_source: str = "OHLC_ADVERSE"
    observed_candles: int
    mae_price: Decimal
    mfe_price: Decimal
    mae_r: Decimal
    mfe_r: Decimal


def _decimal(value: Any, name: str) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite():
        raise ValueError(f"{name} must be finite")
    return converted


def _funding_event_id(logical_signal_id: str, event_type: ReplayEventType, time: datetime) -> str:
    canonical = "|".join(
        (
            REPLAY_VERSION,
            logical_signal_id,
            event_type.value,
            time.isoformat(timespec="milliseconds"),
            "FUNDING",
            time.isoformat(timespec="milliseconds"),
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _open_fraction_at(costed: CostedReplayResult, settlement_time: datetime) -> Decimal:
    fraction = Decimal(1)
    for event in costed.trade.events:
        if event.exchange_time >= settlement_time:
            break
        if event.event_type in {
            ReplayEventType.TP1_FILL,
            ReplayEventType.STOP_FILL,
            ReplayEventType.TP2_FILL,
            ReplayEventType.TIME_EXIT,
        }:
            if event.quantity_fraction is None:
                raise ValueError("position-changing Fill lacks quantity fraction")
            fraction -= event.quantity_fraction
    return fraction


def apply_funding(
    costed: CostedReplayResult,
    dataset: FundingDataset | None,
) -> FundingAdjustedResult:
    """Apply actual Funding or exclude incomplete Net statistics without substituting zero."""

    trade = costed.trade
    closed = (
        trade.armed_result.state is ArmedState.TRIGGERED
        and trade.position_state is PositionState.FLAT
        and trade.armed_result.entry_time is not None
        and trade.exit_time is not None
        and costed.net_pnl_before_funding is not None
    )
    if not closed:
        return FundingAdjustedResult(
            costed=costed,
            applications=(),
            funding_cash_flow=Decimal(0),
            funding_data_missing=False,
            missing_settlement_times=(),
            net_statistics_eligible=False,
            net_pnl=None,
            net_r=None,
        )
    if costed.scenario is CostScenario.ZERO:
        return FundingAdjustedResult(
            costed=costed,
            applications=(),
            funding_cash_flow=Decimal(0),
            funding_data_missing=False,
            missing_settlement_times=(),
            net_statistics_eligible=True,
            net_pnl=costed.net_pnl_before_funding,
            net_r=costed.net_r_before_funding,
        )

    entry_time = cast(datetime, trade.armed_result.entry_time)
    exit_time = cast(datetime, trade.exit_time)
    if (
        dataset is None
        or dataset.coverage_start > entry_time
        or dataset.coverage_end <= exit_time
    ):
        return FundingAdjustedResult(
            costed=costed,
            applications=(),
            funding_cash_flow=Decimal(0),
            funding_data_missing=True,
            missing_settlement_times=(),
            net_statistics_eligible=False,
            net_pnl=None,
            net_r=None,
        )

    relevant_times = tuple(
        time
        for time in dataset.expected_settlement_times
        if entry_time < time < exit_time
    )
    by_time = {item.settlement_time: item for item in dataset.observations}
    applications: list[FundingApplication] = []
    missing: list[datetime] = []
    total = Decimal(0)
    direction_sign = Decimal(-1) if trade.request.armed.direction is Direction.LONG else Decimal(1)
    for settlement_time in relevant_times:
        observation = by_time.get(settlement_time)
        fraction = _open_fraction_at(costed, settlement_time)
        complete = (
            observation is not None
            and observation.rate is not None
            and observation.mark_price is not None
        )
        event_type = (
            ReplayEventType.FUNDING
            if complete
            else ReplayEventType.FUNDING_DATA_MISSING
        )
        cash_flow: Decimal | None = None
        if (
            complete
            and observation is not None
            and observation.rate is not None
            and observation.mark_price is not None
        ):
            cash_flow = (
                direction_sign
                * observation.mark_price
                * observation.rate
                * fraction
            )
            total += cash_flow
        else:
            missing.append(settlement_time)
        applications.append(
            FundingApplication(
                event_id=_funding_event_id(
                    trade.request.armed.logical_signal_id,
                    event_type,
                    settlement_time,
                ),
                event_type=event_type,
                settlement_time=settlement_time,
                rate=observation.rate if observation is not None else None,
                mark_price=observation.mark_price if observation is not None else None,
                open_fraction=fraction,
                cash_flow=cash_flow,
            )
        )

    if missing:
        return FundingAdjustedResult(
            costed=costed,
            applications=tuple(applications),
            funding_cash_flow=total,
            funding_data_missing=True,
            missing_settlement_times=tuple(missing),
            net_statistics_eligible=False,
            net_pnl=None,
            net_r=None,
        )
    net_pnl = cast(Decimal, costed.net_pnl_before_funding) + total
    risk = abs(trade.request.armed.entry_price - trade.request.armed.stop_price)
    return FundingAdjustedResult(
        costed=costed,
        applications=tuple(applications),
        funding_cash_flow=total,
        funding_data_missing=False,
        missing_settlement_times=(),
        net_statistics_eligible=True,
        net_pnl=net_pnl,
        net_r=net_pnl / risk,
    )


def calculate_excursions(
    costed: CostedReplayResult,
    candles: pd.DataFrame,
) -> ExcursionResult | None:
    """Calculate trade-level OHLC fallback MAE/MFE through the final exit Candle."""

    validate_replay_candles(candles)
    trade = costed.trade
    entry_time = trade.armed_result.entry_time
    entry_price = trade.armed_result.theoretical_entry_price
    if entry_time is None or entry_price is None:
        return None
    exit_time = trade.exit_time
    is_time_exit = trade.exit_reason is TradeExitReason.TIME_EXIT
    open_times = pd.DatetimeIndex(candles["open_time"])
    visible = open_times >= pd.Timestamp(entry_time)
    if exit_time is not None:
        visible &= (
            open_times < pd.Timestamp(exit_time)
            if is_time_exit
            else open_times <= pd.Timestamp(exit_time)
        )
    positions = [index for index, selected in enumerate(visible) if selected]
    if not positions:
        return None
    highs = tuple(_decimal(candles["high"].iloc[index], "high") for index in positions)
    lows = tuple(_decimal(candles["low"].iloc[index], "low") for index in positions)
    risk = abs(trade.request.armed.entry_price - trade.request.armed.stop_price)
    if trade.request.armed.direction is Direction.LONG:
        mae_price = min(lows)
        mfe_price = max(highs)
        mae_r = max(Decimal(0), entry_price - mae_price) / risk
        mfe_r = max(Decimal(0), mfe_price - entry_price) / risk
    else:
        mae_price = max(highs)
        mfe_price = min(lows)
        mae_r = max(Decimal(0), mae_price - entry_price) / risk
        mfe_r = max(Decimal(0), entry_price - mfe_price) / risk
    return ExcursionResult(
        observed_candles=len(positions),
        mae_price=mae_price,
        mfe_price=mfe_price,
        mae_r=mae_r,
        mfe_r=mfe_r,
    )
