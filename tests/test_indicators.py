"""Reference and boundary tests for the deterministic numeric indicators."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from slagalpha.strategy.indicators import (
    IndicatorInputError,
    compute_indicator_frame,
    quote_volume_statistics,
    simple_moving_average,
    true_range,
    wilder_atr14,
)


def candle_frame(rows: int) -> pd.DataFrame:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    open_times = [start + timedelta(minutes=15 * index) for index in range(rows)]
    close = np.arange(100.0, 100.0 + rows, dtype=np.float64)
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime(open_times, utc=True),
            "close_time_exclusive": pd.to_datetime(
                [value + timedelta(minutes=15) for value in open_times], utc=True
            ),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "quote_volume": np.arange(1.0, rows + 1.0, dtype=np.float64),
            "is_closed": True,
        }
    )


def test_sma_uses_current_closed_candle_and_full_window() -> None:
    close = pd.Series(np.arange(1.0, 181.0))

    sma30 = simple_moving_average(close, 30)
    sma60 = simple_moving_average(close, 60)
    sma90 = simple_moving_average(close, 90)
    sma180 = simple_moving_average(close, 180)

    assert sma30.iloc[:29].isna().all()
    assert sma30.iloc[29] == pytest.approx(15.5)
    assert sma60.iloc[59] == pytest.approx(30.5)
    assert sma90.iloc[89] == pytest.approx(45.5)
    assert sma180.iloc[179] == pytest.approx(90.5)


def test_unsupported_sma_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        simple_moving_average(pd.Series([1.0, 2.0]), 20)  # type: ignore[arg-type]


def test_true_range_and_wilder_atr_match_hand_calculated_reference() -> None:
    close = pd.Series(
        [100, 101, 102, 101, 104, 103, 107, 106, 108, 107, 110, 109, 111, 115, 114, 118],
        dtype="float64",
    )
    high = close + 1.0
    low = close - 1.0

    ranges = true_range(high, low, close)
    atr = wilder_atr14(high, low, close)

    assert ranges.tolist() == [2, 2, 2, 2, 4, 2, 5, 2, 3, 2, 4, 2, 3, 5, 2, 5]
    assert atr.iloc[:13].isna().all()
    assert atr.iloc[13] == pytest.approx(40 / 14)
    assert atr.iloc[14] == pytest.approx(((40 / 14) * 13 + 2) / 14)
    assert atr.iloc[15] == pytest.approx((atr.iloc[14] * 13 + 5) / 14)


def test_quote_volume_windows_exclude_candidate_candle() -> None:
    volume = pd.Series(np.arange(1.0, 25.0))

    statistics = quote_volume_statistics(volume)

    assert statistics["quote_volume_previous"].iloc[23] == 23
    assert statistics["quote_volume_mean3_prev"].iloc[23] == 22
    assert statistics["quote_volume_median20_prev"].iloc[23] == 13.5
    assert statistics["quote_volume_median20_before_prev3"].iloc[23] == 10.5
    assert np.isnan(statistics["quote_volume_median20_before_prev3"].iloc[22])

    changed = volume.copy()
    changed.iloc[23] = 1_000_000
    changed_statistics = quote_volume_statistics(changed)
    pd.testing.assert_series_equal(statistics.iloc[23], changed_statistics.iloc[23])


def test_indicator_frame_warmup_counts_and_input_immutability() -> None:
    candles = candle_frame(200)
    original = candles.copy(deep=True)

    indicators = compute_indicator_frame(candles)

    assert indicators["sma30"].notna().sum() == 171
    assert indicators["sma60"].notna().sum() == 141
    assert indicators["sma90"].notna().sum() == 111
    assert indicators["sma180"].notna().sum() == 21
    assert indicators["atr14"].notna().sum() == 187
    pd.testing.assert_frame_equal(candles, original)


def test_indicator_prefix_is_unchanged_when_future_rows_are_added() -> None:
    candles = candle_frame(220)

    full = compute_indicator_frame(candles)
    prefix = compute_indicator_frame(candles.iloc[:200].copy())

    pd.testing.assert_frame_equal(full.iloc[:200], prefix)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda frame: frame.assign(is_closed=False), "closed"),
        (lambda frame: frame.assign(high=np.nan), "non-finite"),
        (lambda frame: frame.assign(quote_volume=-1), "non-negative"),
        (lambda frame: frame.iloc[::-1], "strictly increasing"),
    ],
)
def test_invalid_indicator_input_fails_closed(mutation: object, message: str) -> None:
    candles = candle_frame(30)
    mutate = mutation
    assert callable(mutate)
    invalid = mutate(candles)
    with pytest.raises(IndicatorInputError, match=message):
        compute_indicator_frame(invalid)

