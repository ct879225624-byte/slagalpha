"""No-lookahead and strict-boundary tests for pivots and structure zones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from slagalpha.strategy.pivots import (
    PivotEvent,
    PivotType,
    detect_confirmed_pivots,
    merge_pivot_zones,
)


def pivot_candles(lows: list[float], highs: list[float] | None = None) -> pd.DataFrame:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    open_times = [start + timedelta(minutes=15 * index) for index in range(len(lows))]
    resolved_highs = highs if highs is not None else [120.0] * len(lows)
    close = [(low + high) / 2 for low, high in zip(lows, resolved_highs, strict=True)]
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime(open_times, utc=True),
            "close_time_exclusive": pd.to_datetime(
                [value + timedelta(minutes=15) for value in open_times], utc=True
            ),
            "high": resolved_highs,
            "low": lows,
            "close": close,
            "quote_volume": 1_000.0,
            "is_closed": True,
        }
    )


def atr_frame(rows: int, value: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame({"atr14": np.full(rows, value, dtype=np.float64)})


def test_pivot_low_appears_only_after_second_right_candle_closes() -> None:
    lows = [110.0] * 12 + [105.0, 103.0, 100.0, 104.0, 106.0]
    candles = pivot_candles(lows)

    one_right_only = detect_confirmed_pivots(
        candles.iloc[:16].copy(), atr_frame(16), "15m"
    )
    confirmed = detect_confirmed_pivots(candles, atr_frame(17), "15m")
    pivot_lows = [event for event in confirmed if event.kind is PivotType.LOW]

    assert one_right_only == ()
    assert len(pivot_lows) == 1
    assert pivot_lows[0].center_position == 14
    assert pivot_lows[0].confirmed_position == 16
    assert pivot_lows[0].price == 100
    assert pivot_lows[0].confirmed_at == candles["close_time_exclusive"].iloc[16]


def test_right_side_equality_is_allowed_but_left_equality_is_not() -> None:
    right_equal = pivot_candles([110, 110, 105, 103, 100, 100, 101])
    left_equal = pivot_candles([110, 110, 100, 103, 100, 101, 102])

    right_events = detect_confirmed_pivots(right_equal, atr_frame(7), "15m")
    left_events = detect_confirmed_pivots(left_equal, atr_frame(7), "15m")

    assert any(event.kind is PivotType.LOW and event.price == 100 for event in right_events)
    assert not any(
        event.kind is PivotType.LOW and event.center_position == 4 for event in left_events
    )


def test_pivot_with_missing_confirmation_atr_is_not_emitted() -> None:
    candles = pivot_candles([110, 110, 105, 103, 100, 104, 106])
    indicators = atr_frame(7)
    indicators.loc[6, "atr14"] = np.nan

    assert detect_confirmed_pivots(candles, indicators, "15m") == ()


def test_pivot_prefix_events_do_not_change_with_future_data() -> None:
    lows = [110, 108, 105, 107, 109, 106, 102, 105, 108, 104, 100, 103, 109, 107, 101]
    candles = pivot_candles([float(value) for value in lows])
    full = detect_confirmed_pivots(candles, atr_frame(len(candles)), "15m")
    prefix = detect_confirmed_pivots(candles.iloc[:12].copy(), atr_frame(12), "15m")
    cutoff = candles["close_time_exclusive"].iloc[11]

    full_known_at_cutoff = tuple(event for event in full if event.confirmed_at <= cutoff)
    assert full_known_at_cutoff == prefix


def test_only_symmetric_frozen_pivot_windows_are_allowed() -> None:
    candles = pivot_candles([110.0] * 10)
    with pytest.raises(ValueError, match="symmetric"):
        detect_confirmed_pivots(candles, atr_frame(10), "15m", left=2, right=3)


def make_pivot(
    sequence: int,
    price: float,
    *,
    kind: PivotType = PivotType.LOW,
    atr: float = 10.0,
) -> PivotEvent:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    pivot_time = start + timedelta(hours=sequence)
    confirmed_at = pivot_time + timedelta(minutes=45)
    pivot_id = f"{sequence:064x}"
    return PivotEvent(
        pivot_id=pivot_id,
        interval="15m",
        kind=kind,
        pivot_time=pivot_time,
        confirmed_at=confirmed_at,
        center_position=sequence * 4,
        confirmed_position=sequence * 4 + 3,
        price=price,
        atr_at_confirmation=atr,
        left=3,
        right=3,
    )


def test_zone_merges_strictly_inside_point_two_atr() -> None:
    zones = merge_pivot_zones((make_pivot(1, 100.0), make_pivot(2, 101.9)))

    assert len(zones) == 1
    assert zones[0].lower == 100.0
    assert zones[0].upper == 101.9
    assert len(zones[0].members) == 2


def test_zone_does_not_merge_at_exact_point_two_atr() -> None:
    zones = merge_pivot_zones((make_pivot(1, 100.0), make_pivot(2, 102.0)))

    assert len(zones) == 2
    assert [zone.lower for zone in zones] == [100.0, 102.0]


def test_zone_compares_new_pivot_to_last_member() -> None:
    zones = merge_pivot_zones(
        (make_pivot(1, 100.0), make_pivot(2, 101.9), make_pivot(3, 103.8))
    )

    assert len(zones) == 1
    assert zones[0].lower == 100.0
    assert zones[0].upper == 103.8
    assert len(zones[0].members) == 3


def test_high_and_low_pivots_never_share_a_zone() -> None:
    zones = merge_pivot_zones(
        (
            make_pivot(1, 100.0, kind=PivotType.LOW),
            make_pivot(2, 100.5, kind=PivotType.HIGH),
        )
    )

    assert len(zones) == 2
    assert {zone.kind for zone in zones} == {PivotType.LOW, PivotType.HIGH}


def test_zone_ids_and_order_are_deterministic_for_reordered_input() -> None:
    first = make_pivot(1, 100.0)
    second = make_pivot(2, 101.9)

    forward = merge_pivot_zones((first, second))
    reversed_input = merge_pivot_zones((second, first))

    assert forward == reversed_input
