"""GS-050 deterministic canonical replay summary tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from slagalpha.backtest.analytics import FundingDataset, apply_funding, calculate_excursions
from slagalpha.backtest.costs import CostScenario, replay_with_costs
from slagalpha.backtest.replay import REPLAY_VERSION, ArmedReplayRequest, TradeReplayRequest
from slagalpha.backtest.summary import CanonicalReplaySummary, build_canonical_summary
from slagalpha.strategy.setup import Direction


def request(*, scaled_decimals: bool = False) -> TradeReplayRequest:
    entry = Decimal("100.0") if scaled_decimals else Decimal("100")
    return TradeReplayRequest(
        armed=ArmedReplayRequest(
            logical_signal_id="e" * 64,
            direction=Direction.LONG,
            entry_price=entry,
            invalidation_price=Decimal("90.0" if scaled_decimals else "90"),
            stop_price=Decimal("89.0" if scaled_decimals else "89"),
            atr_at_confirmation=Decimal("10.0" if scaled_decimals else "10"),
            confirmation_close=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            expires_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
        ),
        tp1=Decimal("111.0" if scaled_decimals else "111"),
        tp2=Decimal("122.0" if scaled_decimals else "122"),
        tick_size=Decimal("0.10" if scaled_decimals else "0.1"),
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


def canonical(
    scenario: CostScenario, *, scaled_decimals: bool = False
) -> CanonicalReplaySummary:
    frame = candles()
    costed = replay_with_costs(request(scaled_decimals=scaled_decimals), frame, scenario)
    funding = apply_funding(
        costed,
        FundingDataset(
            coverage_start=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            coverage_end=datetime(2024, 1, 1, 10, 2, tzinfo=UTC),
            expected_settlement_times=(),
            observations=(),
        ),
    )
    return build_canonical_summary(funding, calculate_excursions(costed, frame))


def test_gs050_same_input_replay_has_identical_canonical_hash() -> None:
    first = canonical(CostScenario.BASELINE)
    repeated = canonical(CostScenario.BASELINE)
    equivalent_decimal_scale = canonical(
        CostScenario.BASELINE, scaled_decimals=True
    )
    assert first == repeated == equivalent_decimal_scale
    assert len(first.canonical_hash) == 64
    assert first.trade_id == hashlib.sha256(
        f"{REPLAY_VERSION}|{'e' * 64}".encode()
    ).hexdigest()
    assert " " not in first.canonical_json


def test_cost_scenario_changes_canonical_hash_but_not_trade_id() -> None:
    baseline = canonical(CostScenario.BASELINE)
    stress = canonical(CostScenario.STRESS)
    assert baseline.trade_id == stress.trade_id
    assert baseline.canonical_hash != stress.canonical_hash
