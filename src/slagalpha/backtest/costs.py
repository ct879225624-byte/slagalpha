"""Deterministic P7.4 execution costs over replay Fill events."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

import pandas as pd
from pydantic import BaseModel, ConfigDict

from slagalpha.backtest.replay import (
    AggregateTradeBatch,
    ArmedState,
    PositionState,
    ReplayEvent,
    ReplayEventType,
    ReplayInputError,
    TradeReplayRequest,
    TradeReplayResult,
    replay_armed,
    replay_trade,
)
from slagalpha.strategy.plans import ceil_to_tick, floor_to_tick
from slagalpha.strategy.setup import Direction


class CostScenario(StrEnum):
    """Frozen P7 cost scenarios."""

    ZERO = "ZERO"
    BASELINE = "BASELINE"
    STRESS = "STRESS"


class ExecutionSide(StrEnum):
    """Fill side used to apply slippage consistently."""

    BUY = "BUY"
    SELL = "SELL"


class CostRates(BaseModel):
    """Per-Fill decimal rates, where one basis point is 0.0001."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fee_rate: Decimal
    slippage_rate: Decimal


COST_RATES: Mapping[CostScenario, CostRates] = MappingProxyType({
    CostScenario.ZERO: CostRates(fee_rate=Decimal(0), slippage_rate=Decimal(0)),
    CostScenario.BASELINE: CostRates(
        fee_rate=Decimal("0.0006"),
        slippage_rate=Decimal("0.0002"),
    ),
    CostScenario.STRESS: CostRates(
        fee_rate=Decimal("0.0006"),
        slippage_rate=Decimal("0.0005"),
    ),
})


class ExecutionFill(BaseModel):
    """One theoretical Fill transformed by a frozen cost scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: ReplayEventType
    side: ExecutionSide
    quantity_fraction: Decimal
    theoretical_price: Decimal
    simulated_price: Decimal
    fee: Decimal
    slippage_cost: Decimal


class CostedReplayResult(BaseModel):
    """P7.4a Fill ledger and closed-trade PnL before Funding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: CostScenario
    rates: CostRates
    trade: TradeReplayResult
    fills: tuple[ExecutionFill, ...]
    total_fee: Decimal
    total_slippage_cost: Decimal
    gross_pnl: Decimal | None
    simulated_execution_pnl: Decimal | None
    net_pnl_before_funding: Decimal | None
    gross_r: Decimal | None
    net_r_before_funding: Decimal | None


_FILL_EVENT_TYPES = frozenset(
    {
        ReplayEventType.ENTRY_FILL,
        ReplayEventType.TP1_FILL,
        ReplayEventType.STOP_FILL,
        ReplayEventType.TP2_FILL,
        ReplayEventType.TIME_EXIT,
    }
)


def cost_aware_breakeven(
    request: TradeReplayRequest,
    theoretical_entry: Decimal,
    scenario: CostScenario,
) -> Decimal:
    """Return the cost-covering theoretical Stop for the remaining 50% leg."""

    rates = COST_RATES[scenario]
    fee = rates.fee_rate
    slippage = rates.slippage_rate
    if request.armed.direction is Direction.LONG:
        entry_simulated = theoretical_entry * (Decimal(1) + slippage)
        raw = entry_simulated * (Decimal(1) + fee) / (
            (Decimal(1) - slippage) * (Decimal(1) - fee)
        )
        return ceil_to_tick(raw, request.tick_size)
    entry_simulated = theoretical_entry * (Decimal(1) - slippage)
    raw = entry_simulated * (Decimal(1) - fee) / (
        (Decimal(1) + slippage) * (Decimal(1) + fee)
    )
    return floor_to_tick(raw, request.tick_size)


def _side(direction: Direction, event_type: ReplayEventType) -> ExecutionSide:
    is_entry = event_type is ReplayEventType.ENTRY_FILL
    if direction is Direction.LONG:
        return ExecutionSide.BUY if is_entry else ExecutionSide.SELL
    return ExecutionSide.SELL if is_entry else ExecutionSide.BUY


def _execution_fill(
    event: ReplayEvent,
    direction: Direction,
    rates: CostRates,
) -> ExecutionFill:
    if event.theoretical_price is None or event.quantity_fraction is None:
        raise ReplayInputError("Fill event lacks theoretical price or quantity fraction")
    side = _side(direction, event.event_type)
    multiplier = (
        Decimal(1) + rates.slippage_rate
        if side is ExecutionSide.BUY
        else Decimal(1) - rates.slippage_rate
    )
    simulated = event.theoretical_price * multiplier
    fraction = event.quantity_fraction
    return ExecutionFill(
        event_id=event.event_id,
        event_type=event.event_type,
        side=side,
        quantity_fraction=fraction,
        theoretical_price=event.theoretical_price,
        simulated_price=simulated,
        fee=abs(simulated * fraction) * rates.fee_rate,
        slippage_cost=abs(simulated - event.theoretical_price) * fraction,
    )


def _signed_cashflow(fill: ExecutionFill, *, simulated: bool) -> Decimal:
    price = fill.simulated_price if simulated else fill.theoretical_price
    value = price * fill.quantity_fraction
    return -value if fill.side is ExecutionSide.BUY else value


def replay_with_costs(
    request: TradeReplayRequest,
    candles: pd.DataFrame,
    scenario: CostScenario,
    aggregate_trade_batches: tuple[AggregateTradeBatch, ...] = (),
) -> CostedReplayResult:
    """Replay with a cost-aware breakeven and build the per-Fill ledger."""

    rates = COST_RATES[scenario]
    armed = replay_armed(request.armed, candles, aggregate_trade_batches)
    cost_request = request
    if armed.state is ArmedState.TRIGGERED:
        if armed.theoretical_entry_price is None:
            raise ReplayInputError("triggered replay lacks theoretical Entry")
        breakeven = cost_aware_breakeven(
            request,
            armed.theoretical_entry_price,
            scenario,
        )
        cost_request = TradeReplayRequest(
            plan_version=request.plan_version,
            armed=request.armed,
            tp1=request.tp1,
            tp2=request.tp2,
            tick_size=request.tick_size,
            max_holding_bars=request.max_holding_bars,
            breakeven_price=breakeven,
        )
    trade = replay_trade(
        cost_request,
        candles,
        aggregate_trade_batches,
        deadline_fee_rate=rates.fee_rate,
        deadline_slippage_rate=rates.slippage_rate,
    )
    fills = tuple(
        _execution_fill(event, request.armed.direction, rates)
        for event in trade.events
        if event.event_type in _FILL_EVENT_TYPES
    )
    total_fee = sum((fill.fee for fill in fills), start=Decimal(0))
    total_slippage = sum(
        (fill.slippage_cost for fill in fills), start=Decimal(0)
    )
    closed = (
        trade.armed_result.state is ArmedState.TRIGGERED
        and trade.position_state is PositionState.FLAT
    )
    if not closed:
        return CostedReplayResult(
            scenario=scenario,
            rates=rates,
            trade=trade,
            fills=fills,
            total_fee=total_fee,
            total_slippage_cost=total_slippage,
            gross_pnl=None,
            simulated_execution_pnl=None,
            net_pnl_before_funding=None,
            gross_r=None,
            net_r_before_funding=None,
        )

    exit_fraction = sum(
        (
            fill.quantity_fraction
            for fill in fills
            if fill.event_type is not ReplayEventType.ENTRY_FILL
        ),
        start=Decimal(0),
    )
    if len(fills) < 2 or fills[0].event_type is not ReplayEventType.ENTRY_FILL:
        raise ReplayInputError("closed trade lacks the Entry Fill")
    if fills[0].quantity_fraction != Decimal(1) or exit_fraction != Decimal(1):
        raise ReplayInputError("closed trade Fill fractions do not balance to one")
    gross_pnl = sum(
        (_signed_cashflow(fill, simulated=False) for fill in fills),
        start=Decimal(0),
    )
    simulated_pnl = sum(
        (_signed_cashflow(fill, simulated=True) for fill in fills),
        start=Decimal(0),
    )
    net_pnl = simulated_pnl - total_fee
    risk = abs(request.armed.entry_price - request.armed.stop_price)
    if risk <= 0:
        raise ReplayInputError("TradePlan risk_per_unit must be positive")
    return CostedReplayResult(
        scenario=scenario,
        rates=rates,
        trade=trade,
        fills=fills,
        total_fee=total_fee,
        total_slippage_cost=total_slippage,
        gross_pnl=gross_pnl,
        simulated_execution_pnl=simulated_pnl,
        net_pnl_before_funding=net_pnl,
        gross_r=gross_pnl / risk,
        net_r_before_funding=net_pnl / risk,
    )
