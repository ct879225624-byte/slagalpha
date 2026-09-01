"""Boundary tests for the deterministic multi-timeframe setup context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from slagalpha.strategy.indicators import compute_indicator_frame
from slagalpha.strategy.setup import (
    BandPosition,
    CompressionReason,
    DailyContext,
    Direction,
    MaOrder,
    PullbackReason,
    PullbackState,
    Regime,
    SetupContextEvaluation,
    SetupDecisionReason,
    SetupInputError,
    SetupNotReadyError,
    classify_band_position,
    classify_ma_order,
    evaluate_daily_context,
    evaluate_four_hour,
    evaluate_one_hour_pullback,
    evaluate_setup_context,
)


def context_candles(rows: int, close: float = 140.0) -> pd.DataFrame:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    open_times = [start + timedelta(hours=4 * index) for index in range(rows)]
    closes = np.full(rows, close, dtype=np.float64)
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime(open_times, utc=True),
            "high": closes + 20.0,
            "low": closes - 20.0,
            "close": closes,
            "quote_volume": np.full(rows, 1_000.0),
            "is_closed": True,
        }
    )


def bull_indicators(rows: int = 12) -> pd.DataFrame:
    sma180 = np.arange(95.0, 95.0 + rows)
    return pd.DataFrame(
        {
            "sma30": sma180 + 30.0,
            "sma60": sma180 + 20.0,
            "sma90": sma180 + 10.0,
            "sma180": sma180,
            "atr14": np.full(rows, 10.0),
        }
    )


def mirror_indicators(indicators: pd.DataFrame, center: float = 150.0) -> pd.DataFrame:
    mirrored = indicators.copy()
    for column in ("sma30", "sma60", "sma90", "sma180"):
        mirrored[column] = 2 * center - indicators[column]
    return mirrored


def one_hour_context(rows: int = 22) -> tuple[pd.DataFrame, pd.DataFrame]:
    candles = context_candles(rows, close=140.0)
    candles["open"] = 140.0
    candles["high"] = 141.0
    candles["low"] = 139.0
    indicators = pd.DataFrame(
        {
            "sma30": np.full(rows, 130.0),
            "sma60": np.full(rows, 120.0),
            "sma90": np.full(rows, 110.0),
            "sma180": np.full(rows, 100.0),
            "atr14": np.full(rows, 10.0),
            "true_range": np.full(rows, 2.0),
        }
    )
    return candles, indicators


def mirror_one_hour(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    center: float = 150.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    mirrored_candles = candles.copy()
    mirrored_candles["open"] = 2 * center - candles["open"]
    mirrored_candles["close"] = 2 * center - candles["close"]
    mirrored_candles["high"] = 2 * center - candles["low"]
    mirrored_candles["low"] = 2 * center - candles["high"]
    return mirrored_candles, mirror_indicators(indicators, center)


def test_strict_ma_order_and_band_boundaries() -> None:
    assert classify_ma_order(130, 120, 110, 100) is MaOrder.BULL_ORDER
    assert classify_ma_order(70, 80, 90, 100) is MaOrder.BEAR_ORDER
    assert classify_ma_order(120, 120, 110, 100) is MaOrder.MIXED_ORDER
    assert classify_band_position(131, 130, 120, 110, 100) is BandPosition.ABOVE_BAND
    assert classify_band_position(99, 130, 120, 110, 100) is BandPosition.BELOW_BAND
    assert classify_band_position(130, 130, 120, 110, 100) is BandPosition.INSIDE_BAND


def test_four_hour_long_short_mirror_and_direction_regime_independence() -> None:
    candles = context_candles(12, close=150.0)
    bullish = bull_indicators()
    long_result = evaluate_four_hour(candles, bullish)

    bearish = mirror_indicators(bullish)
    bearish_candles = candles.copy()
    bearish_candles["close"] = 2 * 150.0 - candles["close"]
    bearish_candles["high"] = bearish_candles["close"] + 20.0
    bearish_candles["low"] = bearish_candles["close"] - 20.0
    short_result = evaluate_four_hour(bearish_candles, bearish)

    assert long_result.direction is Direction.LONG
    assert short_result.direction is Direction.SHORT
    assert long_result.regime is Regime.TRENDING
    assert short_result.regime is Regime.TRENDING
    assert long_result.band_width == pytest.approx(short_result.band_width)

    compressed = bullish.copy()
    compressed.loc[11, ["sma30", "sma60", "sma90"]] = [112.0, 111.0, 110.0]
    compressed_result = evaluate_four_hour(candles, compressed)
    assert compressed_result.direction is Direction.LONG
    assert compressed_result.regime is Regime.COMPRESSED
    assert CompressionReason.MA_BAND_NARROW in compressed_result.compression_reasons


def test_four_hour_compression_thresholds_are_exact() -> None:
    candles = context_candles(12)
    indicators = bull_indicators()
    indicators.loc[11, ["sma30", "sma60", "sma90", "sma180"]] = [107.5, 105, 102.5, 100]
    indicators.loc[6, "sma180"] = 90

    at_boundary = evaluate_four_hour(candles, indicators, compression_threshold=0.75)
    above_boundary = evaluate_four_hour(candles, indicators, compression_threshold=1.0)

    assert CompressionReason.MA_BAND_NARROW not in at_boundary.compression_reasons
    assert CompressionReason.MA_BAND_NARROW in above_boundary.compression_reasons
    with pytest.raises(SetupInputError, match="one of"):
        evaluate_four_hour(candles, indicators, compression_threshold=0.8)


def test_flat_order_change_and_band_flip_compression_keep_all_reasons() -> None:
    candles = context_candles(12)
    indicators = bull_indicators()
    indicators.loc[6, "sma180"] = indicators.loc[11, "sma180"] - 1.0
    for position in (2, 4, 6):
        indicators.loc[position, "sma30"] = indicators.loc[position, "sma60"]
    candles.loc[[0, 4, 8], "close"] = [200.0, 50.0, 200.0]
    candles.loc[[0, 4, 8], "high"] = candles.loc[[0, 4, 8], "close"] + 20.0
    candles.loc[[0, 4, 8], "low"] = candles.loc[[0, 4, 8], "close"] - 20.0

    result = evaluate_four_hour(candles, indicators)

    assert result.order_change_count >= 3
    assert result.band_flip_count == 2
    assert result.compression_reasons == (
        CompressionReason.MA180_FLAT,
        CompressionReason.MA_ORDER_UNSTABLE,
        CompressionReason.PRICE_BAND_WHIPSAW,
    )


@pytest.mark.parametrize(
    ("direction", "mirror", "expected"),
    [
        (Direction.LONG, False, DailyContext.ALIGNED),
        (Direction.SHORT, False, DailyContext.BLOCK_SHORT),
        (Direction.LONG, True, DailyContext.BLOCK_LONG),
        (Direction.SHORT, True, DailyContext.ALIGNED),
    ],
)
def test_daily_context_alignment_and_opposition_are_symmetric(
    direction: Direction,
    mirror: bool,
    expected: DailyContext,
) -> None:
    candles = context_candles(6)
    indicators = bull_indicators(6)
    if mirror:
        indicators = mirror_indicators(indicators)

    result = evaluate_daily_context(candles, indicators, direction=direction)

    assert result.context is expected


def test_daily_mixed_and_fail_closed_inputs() -> None:
    candles = context_candles(12)
    indicators = bull_indicators()
    indicators.loc[11, "sma30"] = indicators.loc[11, "sma60"]
    result = evaluate_daily_context(candles, indicators, direction=Direction.LONG)
    assert result.context is DailyContext.MIXED

    with pytest.raises(SetupInputError, match="neutral"):
        evaluate_daily_context(candles, indicators, direction=Direction.NEUTRAL)
    with pytest.raises(SetupNotReadyError, match="12"):
        evaluate_four_hour(candles.iloc[:11], indicators.iloc[:11])
    invalid = indicators.copy()
    invalid.loc[11, "atr14"] = 0
    with pytest.raises(SetupInputError, match="greater than zero"):
        evaluate_four_hour(candles, invalid)


def test_four_hour_result_is_prefix_stable_and_repeatable() -> None:
    candles = context_candles(13)
    indicators = bull_indicators(13)

    first = evaluate_four_hour(candles.iloc[:12].copy(), indicators.iloc[:12].copy())
    repeated = evaluate_four_hour(candles.iloc[:12].copy(), indicators.iloc[:12].copy())
    candles.loc[12, ["high", "low", "close"]] = [1_020.0, 980.0, 1_000.0]
    indicators.loc[12, ["sma30", "sma60", "sma90", "sma180"]] = [1.0, 2.0, 3.0, 4.0]
    prefix_after_future_change = evaluate_four_hour(
        candles.iloc[:12].copy(), indicators.iloc[:12].copy()
    )

    assert first == repeated
    assert first == prefix_after_future_change


@pytest.mark.parametrize(
    ("high", "low", "close", "expected"),
    [
        (141.0, 100.0, 140.0, PullbackState.RESET),
        (115.0, 105.0, 109.0, PullbackState.DAMAGED),
        (125.0, 115.0, 124.0, PullbackState.STANDARD),
        (135.0, 125.0, 134.0, PullbackState.SHALLOW),
        (135.0, 133.0, 134.0, PullbackState.WATCHING),
        (141.0, 139.0, 140.0, PullbackState.NONE),
    ],
)
def test_one_hour_state_priority_and_short_mirror(
    high: float,
    low: float,
    close: float,
    expected: PullbackState,
) -> None:
    candles, indicators = one_hour_context()
    candles.loc[21, ["high", "low", "close"]] = [high, low, close]
    long_result = evaluate_one_hour_pullback(
        candles, indicators, symbol="btcusdt", direction=Direction.LONG
    )

    short_candles, short_indicators = mirror_one_hour(candles, indicators)
    short_result = evaluate_one_hour_pullback(
        short_candles, short_indicators, symbol="BTCUSDT", direction=Direction.SHORT
    )

    assert long_result.state is expected
    assert short_result.state is expected
    assert long_result.symbol == "BTCUSDT"


def test_standard_precedes_shallow_and_wick_is_not_damaged() -> None:
    candles, indicators = one_hour_context()
    candles.loc[21, ["high", "low", "close"]] = [131.0, 105.0, 125.0]

    result = evaluate_one_hour_pullback(
        candles, indicators, symbol="BTCUSDT", direction=Direction.LONG
    )

    assert result.state is PullbackState.STANDARD
    assert PullbackReason.PULLBACK_DAMAGED not in result.reason_codes


def test_acceleration_requires_all_strict_conditions() -> None:
    candles, indicators = one_hour_context()
    candles.loc[20, ["open", "high", "low", "close", "quote_volume"]] = [
        130.0,
        131.0,
        115.0,
        125.0,
        2_000.0,
    ]
    candles.loc[21, ["open", "high", "low", "close", "quote_volume"]] = [
        130.0,
        125.0,
        115.0,
        124.0,
        2_000.0,
    ]
    indicators.loc[[20, 21], "true_range"] = [5.0, 6.0]

    accelerating = evaluate_one_hour_pullback(
        candles, indicators, symbol="BTCUSDT", direction=Direction.LONG
    )
    assert accelerating.accelerating is True
    assert accelerating.eligible_for_trigger is False

    candles.loc[21, "quote_volume"] = 1_000.0
    equal_volume = evaluate_one_hour_pullback(
        candles, indicators, symbol="BTCUSDT", direction=Direction.LONG
    )
    assert equal_volume.accelerating is False

    candles.loc[21, "quote_volume"] = 2_000.0
    indicators.loc[21, "true_range"] = 5.0
    equal_range = evaluate_one_hour_pullback(
        candles, indicators, symbol="BTCUSDT", direction=Direction.LONG
    )
    assert equal_range.accelerating is False


def test_episode_id_is_stable_across_continuation_and_requires_trend_side_predecessor() -> None:
    candles, indicators = one_hour_context(23)
    candles.loc[21, ["open", "high", "low", "close"]] = [125.0, 126.0, 115.0, 124.0]
    candles.loc[22, ["open", "high", "low", "close"]] = [124.0, 125.0, 115.0, 123.0]

    started = evaluate_one_hour_pullback(
        candles.iloc[:22].copy(),
        indicators.iloc[:22].copy(),
        symbol="BTCUSDT",
        direction=Direction.LONG,
    )
    continued = evaluate_one_hour_pullback(
        candles, indicators, symbol="BTCUSDT", direction=Direction.LONG
    )

    assert started.episode_is_new is True
    assert started.eligible_for_trigger is True
    assert started.episode_id == continued.episode_id
    assert started.episode_start == continued.episode_start

    candles.loc[20, ["open", "high", "low", "close"]] = [105.0, 106.0, 101.0, 105.0]
    not_new = evaluate_one_hour_pullback(
        candles.iloc[:22].copy(),
        indicators.iloc[:22].copy(),
        symbol="BTCUSDT",
        direction=Direction.LONG,
    )
    assert not_new.episode_is_new is False
    assert not_new.eligible_for_trigger is False
    assert PullbackReason.PULLBACK_EPISODE_NOT_NEW in not_new.reason_codes


def test_pullback_invalid_ma_direction_and_input_fail_closed() -> None:
    candles, indicators = one_hour_context()
    indicators.loc[21, "sma30"] = indicators.loc[21, "sma60"]
    result = evaluate_one_hour_pullback(
        candles, indicators, symbol="BTCUSDT", direction=Direction.LONG
    )
    assert result.state is PullbackState.NONE
    assert result.ma_direction_valid is False
    assert PullbackReason.MA_DIRECTION_INVALID in result.reason_codes

    with pytest.raises(SetupNotReadyError, match="22"):
        evaluate_one_hour_pullback(
            candles.iloc[:21],
            indicators.iloc[:21],
            symbol="BTCUSDT",
            direction=Direction.LONG,
        )
    with pytest.raises(SetupInputError, match="neutral"):
        evaluate_one_hour_pullback(
            candles, indicators, symbol="BTCUSDT", direction=Direction.NEUTRAL
        )


def test_setup_context_short_circuits_in_frozen_order() -> None:
    four_hour_candles = context_candles(12)
    empty = pd.DataFrame()

    neutral_indicators = bull_indicators()
    neutral_indicators.loc[11, ["sma30", "sma60", "sma90"]] = [112.0, 112.0, 110.0]
    neutral = evaluate_setup_context(
        symbol="BTCUSDT",
        four_hour_candles=four_hour_candles,
        four_hour_indicators=neutral_indicators,
        daily_candles=empty,
        daily_indicators=empty,
        one_hour_candles=empty,
        one_hour_indicators=empty,
    )
    assert neutral.decision_reason is SetupDecisionReason.FOUR_HOUR_NEUTRAL
    assert neutral.daily is None
    assert neutral.one_hour is None

    compressed_indicators = bull_indicators()
    compressed_indicators.loc[11, ["sma30", "sma60", "sma90"]] = [112.0, 111.0, 110.0]
    compressed = evaluate_setup_context(
        symbol="BTCUSDT",
        four_hour_candles=four_hour_candles,
        four_hour_indicators=compressed_indicators,
        daily_candles=empty,
        daily_indicators=empty,
        one_hour_candles=empty,
        one_hour_indicators=empty,
    )
    assert compressed.decision_reason is SetupDecisionReason.FOUR_HOUR_COMPRESSED
    assert compressed.daily is None
    assert compressed.one_hour is None

    daily_candles = context_candles(6)
    blocked = evaluate_setup_context(
        symbol="BTCUSDT",
        four_hour_candles=four_hour_candles,
        four_hour_indicators=bull_indicators(),
        daily_candles=daily_candles,
        daily_indicators=mirror_indicators(bull_indicators(6)),
        one_hour_candles=empty,
        one_hour_indicators=empty,
    )
    assert blocked.decision_reason is SetupDecisionReason.DAILY_BLOCK_LONG
    assert blocked.daily is not None
    assert blocked.one_hour is None


def test_setup_context_reaches_eligible_one_hour_candidate() -> None:
    one_hour_candles, one_hour_indicators = one_hour_context()
    one_hour_candles.loc[21, ["open", "high", "low", "close"]] = [
        125.0,
        126.0,
        115.0,
        124.0,
    ]

    result = evaluate_setup_context(
        symbol="btcusdt",
        four_hour_candles=context_candles(12),
        four_hour_indicators=bull_indicators(),
        daily_candles=context_candles(6),
        daily_indicators=bull_indicators(6),
        one_hour_candles=one_hour_candles,
        one_hour_indicators=one_hour_indicators,
    )

    assert result.decision_reason is SetupDecisionReason.ONE_HOUR_EVALUATED
    assert result.eligible_for_trigger is True
    assert result.daily is not None
    assert result.daily.context is DailyContext.ALIGNED
    assert result.one_hour is not None
    assert result.one_hour.state is PullbackState.STANDARD


def test_setup_context_complete_short_path_and_daily_block() -> None:
    long_four_hour_candles = context_candles(12, close=150.0)
    long_four_hour_indicators = bull_indicators()
    four_hour_candles, four_hour_indicators = mirror_one_hour(
        long_four_hour_candles.assign(open=150.0), long_four_hour_indicators
    )
    daily_candles, daily_indicators = mirror_one_hour(
        context_candles(6, close=150.0).assign(open=150.0), bull_indicators(6)
    )
    one_hour_candles, one_hour_indicators = one_hour_context()
    one_hour_candles.loc[21, ["open", "high", "low", "close"]] = [
        125.0,
        126.0,
        115.0,
        124.0,
    ]
    short_one_hour_candles, short_one_hour_indicators = mirror_one_hour(
        one_hour_candles, one_hour_indicators
    )

    result = evaluate_setup_context(
        symbol="BTCUSDT",
        four_hour_candles=four_hour_candles,
        four_hour_indicators=four_hour_indicators,
        daily_candles=daily_candles,
        daily_indicators=daily_indicators,
        one_hour_candles=short_one_hour_candles,
        one_hour_indicators=short_one_hour_indicators,
    )
    assert result.four_hour.direction is Direction.SHORT
    assert result.daily is not None
    assert result.daily.context is DailyContext.ALIGNED
    assert result.one_hour is not None
    assert result.one_hour.state is PullbackState.STANDARD
    assert result.eligible_for_trigger is True

    blocked = evaluate_setup_context(
        symbol="BTCUSDT",
        four_hour_candles=four_hour_candles,
        four_hour_indicators=four_hour_indicators,
        daily_candles=context_candles(6),
        daily_indicators=bull_indicators(6),
        one_hour_candles=pd.DataFrame(),
        one_hour_indicators=pd.DataFrame(),
    )
    assert blocked.decision_reason is SetupDecisionReason.DAILY_BLOCK_SHORT
    assert blocked.one_hour is None


def trend_candles(rows: int, interval_hours: int) -> pd.DataFrame:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    close = np.arange(100.0, 100.0 + rows)
    open_price = close - 0.25
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime(
                [start + timedelta(hours=interval_hours * index) for index in range(rows)],
                utc=True,
            ),
            "open": open_price,
            "high": close + 1.0,
            "low": open_price - 1.0,
            "close": close,
            "quote_volume": np.full(rows, 1_000.0),
            "is_closed": True,
        }
    )


def test_deterministic_multi_timeframe_replay_uses_computed_indicators() -> None:
    four_hour_candles = trend_candles(241, 4)
    daily_candles = trend_candles(241, 24)
    one_hour_candles = trend_candles(241, 1)
    one_hour_candles.loc[239, ["open", "high", "low", "close"]] = [
        305.0,
        310.0,
        295.0,
        300.0,
    ]
    four_hour_indicators = compute_indicator_frame(four_hour_candles)
    daily_indicators = compute_indicator_frame(daily_candles)
    one_hour_indicators = compute_indicator_frame(one_hour_candles)

    def replay() -> SetupContextEvaluation:
        return evaluate_setup_context(
            symbol="BTCUSDT",
            four_hour_candles=four_hour_candles.iloc[:240].copy(),
            four_hour_indicators=four_hour_indicators.iloc[:240].copy(),
            daily_candles=daily_candles.iloc[:240].copy(),
            daily_indicators=daily_indicators.iloc[:240].copy(),
            one_hour_candles=one_hour_candles.iloc[:240].copy(),
            one_hour_indicators=one_hour_indicators.iloc[:240].copy(),
        )

    first = replay()
    second = replay()

    assert first == second
    assert first.four_hour.direction is Direction.LONG
    assert first.four_hour.regime is Regime.TRENDING
    assert first.daily is not None
    assert first.daily.context is DailyContext.ALIGNED
    assert first.one_hour is not None
    assert first.one_hour.state is PullbackState.STANDARD
    assert first.eligible_for_trigger is True
