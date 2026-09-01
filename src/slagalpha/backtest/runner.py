"""Minimal deterministic multi-symbol P7 replay scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import pandas as pd
from pydantic import BaseModel, ConfigDict

from slagalpha.backtest.costs import CostedReplayResult, CostScenario, replay_with_costs
from slagalpha.backtest.replay import (
    AggregateTradeBatch,
    ArmedState,
    ReplayInputError,
    TradeReplayRequest,
)


class ReplayCaseStatus(StrEnum):
    """Scheduler result for one logical signal."""

    EXECUTED = "EXECUTED"
    SKIPPED_ACTIVE_TRADE = "SKIPPED_ACTIVE_TRADE"


@dataclass(frozen=True)
class ReplayCase:
    """One independently validated signal and its local market data."""

    request: TradeReplayRequest
    candles: pd.DataFrame
    scenario: CostScenario
    aggregate_trade_batches: tuple[AggregateTradeBatch, ...] = ()


class ReplayCaseResult(BaseModel):
    """Deterministically ordered scheduler output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_signal_id: str
    symbol: str
    confirmation_close: datetime
    status: ReplayCaseStatus
    blocked_by_logical_signal_id: str | None
    replay: CostedReplayResult | None


class MultiSymbolReplayResult(BaseModel):
    """Ordered results with no more than one active trade per symbol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: tuple[ReplayCaseResult, ...]


def replay_cases(cases: tuple[ReplayCase, ...]) -> MultiSymbolReplayResult:
    """Run cases by event time, symbol and logical ID with active-trade exclusion."""

    signal_ids = tuple(case.request.armed.logical_signal_id for case in cases)
    if len(set(signal_ids)) != len(signal_ids):
        raise ReplayInputError("multi-symbol replay logical_signal_id values must be unique")
    if any(case.request.armed.symbol == "UNKNOWN" for case in cases):
        raise ReplayInputError("multi-symbol replay requires an explicit symbol")
    ordered = sorted(
        cases,
        key=lambda case: (
            case.request.armed.confirmation_close,
            case.request.armed.symbol,
            case.request.armed.logical_signal_id,
        ),
    )
    active_until: dict[str, datetime | None] = {}
    active_signal: dict[str, str] = {}
    results: list[ReplayCaseResult] = []
    for case in ordered:
        request = case.request
        symbol = request.armed.symbol
        confirmation = request.armed.confirmation_close
        if symbol in active_until:
            prior_exit = active_until[symbol]
            if prior_exit is None or confirmation < prior_exit:
                results.append(
                    ReplayCaseResult(
                        logical_signal_id=request.armed.logical_signal_id,
                        symbol=symbol,
                        confirmation_close=confirmation,
                        status=ReplayCaseStatus.SKIPPED_ACTIVE_TRADE,
                        blocked_by_logical_signal_id=active_signal[symbol],
                        replay=None,
                    )
                )
                continue
        replay = replay_with_costs(
            request,
            case.candles,
            case.scenario,
            case.aggregate_trade_batches,
        )
        results.append(
            ReplayCaseResult(
                logical_signal_id=request.armed.logical_signal_id,
                symbol=symbol,
                confirmation_close=confirmation,
                status=ReplayCaseStatus.EXECUTED,
                blocked_by_logical_signal_id=None,
                replay=replay,
            )
        )
        if replay.trade.armed_result.state is ArmedState.TRIGGERED:
            active_until[symbol] = replay.trade.exit_time
            active_signal[symbol] = request.armed.logical_signal_id
    return MultiSymbolReplayResult(cases=tuple(results))
