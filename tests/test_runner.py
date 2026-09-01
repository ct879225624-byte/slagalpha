"""Deterministic multi-symbol P7 scheduler tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from slagalpha.backtest.costs import CostScenario
from slagalpha.backtest.replay import ArmedReplayRequest, TradeReplayRequest
from slagalpha.backtest.runner import ReplayCase, ReplayCaseStatus, replay_cases
from slagalpha.strategy.setup import Direction


def request(symbol: str, signal_character: str) -> TradeReplayRequest:
    return TradeReplayRequest(
        armed=ArmedReplayRequest(
            logical_signal_id=signal_character * 64,
            symbol=symbol,
            direction=Direction.LONG,
            entry_price=Decimal("100"),
            invalidation_price=Decimal("90"),
            stop_price=Decimal("89"),
            atr_at_confirmation=Decimal("10"),
            confirmation_close=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            expires_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
        ),
        tp1=Decimal("111"),
        tp2=Decimal("122"),
        tick_size=Decimal("0.1"),
        max_holding_bars=16,
    )


def candles() -> pd.DataFrame:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    rows = [
        (start, 99, 101, 98, 100),
        (start + timedelta(minutes=1), 105, 123, 99, 120),
    ]
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime([row[0] for row in rows], utc=True),
            "close_time_exclusive": pd.to_datetime(
                [row[0] + timedelta(minutes=1) for row in rows], utc=True
            ),
            "open": [row[1] for row in rows],
            "high": [row[2] for row in rows],
            "low": [row[3] for row in rows],
            "close": [row[4] for row in rows],
            "quote_volume": 1_000.0,
            "is_closed": True,
        }
    )


def test_same_time_cases_are_sorted_and_same_symbol_is_blocked() -> None:
    frame = candles()
    cases = (
        ReplayCase(request("ETHUSDT", "c"), frame, CostScenario.ZERO),
        ReplayCase(request("BTCUSDT", "b"), frame, CostScenario.ZERO),
        ReplayCase(request("BTCUSDT", "a"), frame, CostScenario.ZERO),
    )
    first = replay_cases(cases)
    repeated = replay_cases(tuple(reversed(cases)))
    assert first == repeated
    assert [(item.symbol, item.logical_signal_id[0], item.status) for item in first.cases] == [
        ("BTCUSDT", "a", ReplayCaseStatus.EXECUTED),
        ("BTCUSDT", "b", ReplayCaseStatus.SKIPPED_ACTIVE_TRADE),
        ("ETHUSDT", "c", ReplayCaseStatus.EXECUTED),
    ]
    assert first.cases[1].blocked_by_logical_signal_id == "a" * 64
    assert first.cases[1].replay is None
