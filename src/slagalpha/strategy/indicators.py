"""Deterministic numeric indicators over validated, closed candles."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

INDICATOR_VERSION = "indicators/0.1.0"
SMA_WINDOWS = (30, 60, 90, 180)
ATR_PERIOD = 14

SmaWindow = Literal[30, 60, 90, 180]


class IndicatorInputError(ValueError):
    """Raised when indicator input cannot be evaluated safely."""


def _finite_float_array(values: Sequence[object] | pd.Series, name: str) -> npt.NDArray[np.float64]:
    try:
        array = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise IndicatorInputError(f"{name} contains a non-numeric value") from error
    if array.ndim != 1:
        raise IndicatorInputError(f"{name} must be one-dimensional")
    if not bool(np.isfinite(array).all()):
        raise IndicatorInputError(f"{name} contains a non-finite value")
    return array


def validate_indicator_input(candles: pd.DataFrame) -> None:
    """Validate the minimum closed-Candle contract required by P3."""

    required = {"open_time", "high", "low", "close", "quote_volume", "is_closed"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise IndicatorInputError(f"missing required columns: {missing}")
    if candles.empty:
        raise IndicatorInputError("candles must not be empty")

    timestamps = pd.DatetimeIndex(candles["open_time"])
    if timestamps.tz is None:
        raise IndicatorInputError("open_time must be timezone-aware")
    if not timestamps.is_monotonic_increasing or timestamps.has_duplicates:
        raise IndicatorInputError("open_time must be unique and strictly increasing")
    if not bool(candles["is_closed"].astype(bool).all()):
        raise IndicatorInputError("all candles must be closed")

    high = _finite_float_array(candles["high"], "high")
    low = _finite_float_array(candles["low"], "low")
    close = _finite_float_array(candles["close"], "close")
    quote_volume = _finite_float_array(candles["quote_volume"], "quote_volume")
    if bool((high < low).any()):
        raise IndicatorInputError("high must be greater than or equal to low")
    if bool(((close < low) | (close > high)).any()):
        raise IndicatorInputError("close must be between low and high")
    if bool((quote_volume < 0).any()):
        raise IndicatorInputError("quote_volume must be non-negative")


def simple_moving_average(close: Sequence[object] | pd.Series, window: SmaWindow) -> pd.Series:
    """Return a strict full-window SMA aligned to the input index."""

    if window not in SMA_WINDOWS:
        raise ValueError(f"unsupported SMA window: {window}")
    values = _finite_float_array(close, "close")
    index = close.index if isinstance(close, pd.Series) else None
    return pd.Series(values, index=index, dtype="float64").rolling(
        window=window,
        min_periods=window,
    ).mean().rename(f"sma{window}")


def true_range(
    high: Sequence[object] | pd.Series,
    low: Sequence[object] | pd.Series,
    close: Sequence[object] | pd.Series,
) -> pd.Series:
    """Return True Range, using high-low for the first row."""

    high_values = _finite_float_array(high, "high")
    low_values = _finite_float_array(low, "low")
    close_values = _finite_float_array(close, "close")
    if not (len(high_values) == len(low_values) == len(close_values)):
        raise IndicatorInputError("high, low, and close lengths must match")
    if bool((high_values < low_values).any()):
        raise IndicatorInputError("high must be greater than or equal to low")

    result = high_values - low_values
    if len(result) > 1:
        previous_close = close_values[:-1]
        result[1:] = np.maximum.reduce(
            (
                result[1:],
                np.abs(high_values[1:] - previous_close),
                np.abs(low_values[1:] - previous_close),
            )
        )
    index = high.index if isinstance(high, pd.Series) else None
    return pd.Series(result, index=index, dtype="float64", name="true_range")


def wilder_atr14(
    high: Sequence[object] | pd.Series,
    low: Sequence[object] | pd.Series,
    close: Sequence[object] | pd.Series,
) -> pd.Series:
    """Return ATR14 with an SMA seed and Wilder recursive smoothing."""

    ranges = true_range(high, low, close)
    range_values = ranges.to_numpy(dtype=np.float64, copy=False)
    result = np.full(len(range_values), np.nan, dtype=np.float64)
    if len(range_values) >= ATR_PERIOD:
        result[ATR_PERIOD - 1] = float(np.mean(range_values[:ATR_PERIOD]))
        for index in range(ATR_PERIOD, len(range_values)):
            result[index] = (
                result[index - 1] * (ATR_PERIOD - 1) + range_values[index]
            ) / ATR_PERIOD
    return pd.Series(result, index=ranges.index, dtype="float64", name="atr14")


def quote_volume_statistics(quote_volume: Sequence[object] | pd.Series) -> pd.DataFrame:
    """Return only past-looking volume windows required by the frozen rules."""

    values = _finite_float_array(quote_volume, "quote_volume")
    if bool((values < 0).any()):
        raise IndicatorInputError("quote_volume must be non-negative")
    index = quote_volume.index if isinstance(quote_volume, pd.Series) else None
    series = pd.Series(values, index=index, dtype="float64")
    previous = series.shift(1)
    return pd.DataFrame(
        {
            "quote_volume_previous": previous,
            "quote_volume_mean3_prev": previous.rolling(3, min_periods=3).mean(),
            "quote_volume_median20_prev": previous.rolling(20, min_periods=20).median(),
            "quote_volume_median20_before_prev3": series.shift(4)
            .rolling(20, min_periods=20)
            .median(),
        },
        index=index,
    )


def compute_indicator_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """Compute the complete P3 numeric indicator frame without mutating candles."""

    validate_indicator_input(candles)
    result = pd.DataFrame(index=candles.index)
    for window in SMA_WINDOWS:
        typed_window = cast(SmaWindow, window)
        result[f"sma{window}"] = simple_moving_average(candles["close"], typed_window)
    result["true_range"] = true_range(candles["high"], candles["low"], candles["close"])
    result["atr14"] = wilder_atr14(candles["high"], candles["low"], candles["close"])
    volume = quote_volume_statistics(candles["quote_volume"])
    return pd.concat([result, volume], axis=1)
