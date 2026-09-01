"""Golden and boundary tests for deterministic 15m triggers."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from slagalpha.strategy.indicators import quote_volume_statistics
from slagalpha.strategy.pivots import PivotEvent, PivotType, PivotZone
from slagalpha.strategy.setup import Direction, PullbackState
from slagalpha.strategy.triggers import (
    TriggerAEvaluation,
    TriggerBEvaluation,
    TriggerInputError,
    TriggerNotReadyError,
    TriggerReason,
    TriggerSetupContext,
    TriggerType,
    evaluate_trigger_a,
    evaluate_trigger_b,
    evaluate_triggers,
    merge_trigger_evaluations,
)


def trigger_a_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = 24
    start = datetime(2024, 1, 1, tzinfo=UTC)
    open_times = [start + timedelta(minutes=15 * position) for position in range(rows)]
    candles = pd.DataFrame(
        {
            "open_time": pd.to_datetime(open_times, utc=True),
            "close_time_exclusive": pd.to_datetime(
                [value + timedelta(minutes=15) for value in open_times], utc=True
            ),
            "high": np.full(rows, 116.0),
            "low": np.full(rows, 114.0),
            "close": np.full(rows, 115.0),
            "quote_volume": np.array([1_000.0] * 20 + [800.0] * 3 + [1_500.0]),
            "is_closed": True,
        }
    )
    candles.loc[20:21, ["high", "low", "close"]] = [110.0, 108.0, 109.0]
    candles.loc[22, ["high", "low", "close"]] = [110.5, 108.0, 109.0]
    candles.loc[23, ["high", "low", "close"]] = [112.0, 100.0, 111.0]
    indicators = pd.DataFrame({"sma30": np.full(rows, 110.0)})
    return candles, pd.concat(
        [indicators, quote_volume_statistics(candles["quote_volume"])], axis=1
    )


def setup_context(direction: Direction = Direction.LONG) -> TriggerSetupContext:
    region = (100.0, 110.0) if direction is Direction.LONG else (90.0, 100.0)
    return TriggerSetupContext(
        symbol="btcusdt",
        direction=direction,
        pullback_state=PullbackState.SHALLOW,
        eligible_for_trigger=True,
        episode_id="a" * 64,
        episode_start=datetime(2024, 1, 1, tzinfo=UTC),
        region_lower=region[0],
        region_upper=region[1],
    )


def structure_pivot(
    candles: pd.DataFrame,
    kind: PivotType = PivotType.LOW,
    *,
    confirmed_at: datetime | None = None,
) -> PivotEvent:
    pivot_time = pd.Timestamp(candles["open_time"].iloc[10]).to_pydatetime()
    confirmation = confirmed_at or pd.Timestamp(
        candles["close_time_exclusive"].iloc[12]
    ).to_pydatetime()
    return PivotEvent(
        pivot_id=("b" if kind is PivotType.LOW else "c") * 64,
        interval="15m",
        kind=kind,
        pivot_time=pivot_time,
        confirmed_at=confirmation,
        center_position=10,
        confirmed_position=12,
        price=98.0 if kind is PivotType.LOW else 102.0,
        atr_at_confirmation=10.0,
        left=2,
        right=2,
    )


def mirror_prices(
    candles: pd.DataFrame, indicators: pd.DataFrame, center: float = 100.0
) -> tuple[pd.DataFrame, pd.DataFrame]:
    mirrored_candles = candles.copy()
    mirrored_candles["high"] = 2 * center - candles["low"]
    mirrored_candles["low"] = 2 * center - candles["high"]
    mirrored_candles["close"] = 2 * center - candles["close"]
    mirrored_indicators = indicators.copy()
    mirrored_indicators["sma30"] = 2 * center - indicators["sma30"]
    return mirrored_candles, mirrored_indicators


def trigger_b_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = 97
    start = datetime(2024, 1, 1, tzinfo=UTC)
    open_times = [start + timedelta(minutes=15 * position) for position in range(rows)]
    volumes = np.array([1_000.0] * 93 + [800.0] * 3 + [1_500.0])
    candles = pd.DataFrame(
        {
            "open_time": pd.to_datetime(open_times, utc=True),
            "close_time_exclusive": pd.to_datetime(
                [value + timedelta(minutes=15) for value in open_times], utc=True
            ),
            "high": np.full(rows, 116.0),
            "low": np.full(rows, 114.0),
            "close": np.full(rows, 115.0),
            "quote_volume": volumes,
            "is_closed": True,
        }
    )
    candles.loc[95, ["high", "low", "close"]] = [110.5, 108.0, 109.0]
    candles.loc[96, ["high", "low", "close"]] = [112.0, 99.0, 111.0]
    indicators = pd.DataFrame({"sma30": np.full(rows, 110.0)})
    return candles, pd.concat(
        [indicators, quote_volume_statistics(candles["quote_volume"])], axis=1
    )


def sweep_zone(
    candles: pd.DataFrame,
    kind: PivotType = PivotType.LOW,
    *,
    confirmed_position: int = 22,
    zone_id_character: str = "d",
) -> PivotZone:
    center_positions = (10, confirmed_position - 2)
    prices = (100.0, 101.0) if kind is PivotType.LOW else (100.0, 99.0)
    members = tuple(
        PivotEvent(
            pivot_id=character * 64,
            interval="15m",
            kind=kind,
            pivot_time=pd.Timestamp(candles["open_time"].iloc[center]).to_pydatetime(),
            confirmed_at=pd.Timestamp(
                candles["close_time_exclusive"].iloc[center + 2]
            ).to_pydatetime(),
            center_position=center,
            confirmed_position=center + 2,
            price=price,
            atr_at_confirmation=10.0,
            left=2,
            right=2,
        )
        for center, price, character in zip(
            center_positions,
            prices,
            ("e", "f"),
            strict=True,
        )
    )
    return PivotZone(
        zone_id=zone_id_character * 64,
        interval="15m",
        kind=kind,
        lower=min(prices),
        upper=max(prices),
        confirmed_at=members[-1].confirmed_at,
        members=members,
    )


def test_gs021_trigger_a_long_and_short_mirror_confirm() -> None:
    candles, indicators = trigger_a_inputs()
    long_result = evaluate_trigger_a(
        candles,
        indicators,
        setup=setup_context(),
        pivots=(structure_pivot(candles),),
    )

    short_candles, short_indicators = mirror_prices(candles, indicators)
    short_result = evaluate_trigger_a(
        short_candles,
        short_indicators,
        setup=setup_context(Direction.SHORT),
        pivots=(structure_pivot(candles, PivotType.HIGH),),
    )

    assert long_result.confirmed is True
    assert short_result.confirmed is True
    assert long_result.reason_codes == (TriggerReason.TRIGGER_A_CONFIRMED,)
    assert short_result.reason_codes == (TriggerReason.TRIGGER_A_CONFIRMED,)
    assert long_result.invalidation_price == 98.0
    assert short_result.invalidation_price == 102.0


def test_gs022_equal_pullback_volume_is_not_contracting() -> None:
    candles, _ = trigger_a_inputs()
    candles.loc[20:22, "quote_volume"] = 1_000.0
    indicators = pd.DataFrame({"sma30": np.full(24, 110.0)})
    indicators = pd.concat(
        [indicators, quote_volume_statistics(candles["quote_volume"])], axis=1
    )

    result = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=(structure_pivot(candles),)
    )

    assert result.confirmed is False
    assert result.evidence.volume.pullback_mean == 1_000.0
    assert result.evidence.volume.pullback_baseline == 1_000.0
    assert result.reason_codes == (TriggerReason.PULLBACK_VOLUME_NOT_CONTRACTING,)


@pytest.mark.parametrize(
    ("direction", "close", "expected_location"),
    [
        (Direction.LONG, 113.0, 0.65),
        (Direction.SHORT, 87.0, 0.35),
    ],
)
def test_gs023_close_location_boundary_passes_without_rounding(
    direction: Direction,
    close: float,
    expected_location: float,
) -> None:
    candles, indicators = trigger_a_inputs()
    candles.loc[23, ["high", "low", "close"]] = [120.0, 100.0, 113.0]
    pivot = structure_pivot(candles)
    if direction is Direction.SHORT:
        candles, indicators = mirror_prices(candles, indicators)
        pivot = structure_pivot(candles, PivotType.HIGH)

    result = evaluate_trigger_a(
        candles, indicators, setup=setup_context(direction), pivots=(pivot,)
    )

    assert result.evidence.volume.close_location == pytest.approx(expected_location)
    assert result.evidence.volume.close_location_favorable is True
    assert result.confirmed is True


def test_gs024_adverse_close_is_rejected_at_raw_value() -> None:
    candles, indicators = trigger_a_inputs()
    candles.loc[23, ["high", "low", "close"]] = [120.0, 100.0, 112.9]

    result = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=(structure_pivot(candles),)
    )

    assert result.evidence.volume.close_location == pytest.approx(0.645)
    assert result.reason_codes == (TriggerReason.VOLUME_WITH_ADVERSE_CLOSE,)


def test_gs025_zero_range_has_no_adverse_close_reason() -> None:
    candles, indicators = trigger_a_inputs()
    candles.loc[23, ["high", "low", "close"]] = [111.0, 111.0, 111.0]

    result = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=(structure_pivot(candles),)
    )

    assert result.evidence.volume.close_location is None
    assert TriggerReason.ZERO_RANGE_TRIGGER in result.reason_codes
    assert TriggerReason.VOLUME_WITH_ADVERSE_CLOSE not in result.reason_codes


def test_gs026_missing_or_future_pivot_cannot_confirm() -> None:
    candles, indicators = trigger_a_inputs()
    confirmation_close = pd.Timestamp(candles["close_time_exclusive"].iloc[-1]).to_pydatetime()
    future_pivot = structure_pivot(
        candles, confirmed_at=confirmation_close + timedelta(minutes=15)
    )

    missing = evaluate_trigger_a(candles, indicators, setup=setup_context(), pivots=())
    future = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=(future_pivot,)
    )

    assert missing.reason_codes == (TriggerReason.STOP_STRUCTURE_MISSING,)
    assert future == missing


def test_reclaim_and_previous_break_are_strict() -> None:
    candles, indicators = trigger_a_inputs()
    indicators.loc[23, "sma30"] = 111.0
    equal_sma = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=(structure_pivot(candles),)
    )
    assert TriggerReason.MA_RECLAIM_FAILED in equal_sma.reason_codes

    indicators.loc[23, "sma30"] = 110.0
    candles.loc[22, "high"] = 111.0
    equal_previous = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=(structure_pivot(candles),)
    )
    assert TriggerReason.PREVIOUS_BAR_BREAK_FAILED in equal_previous.reason_codes


def test_pullback_region_boundary_touch_counts() -> None:
    candles, indicators = trigger_a_inputs()
    candles.loc[19:22, ["high", "low", "close"]] = [116.0, 114.0, 115.0]
    candles.loc[20, ["high", "low", "close"]] = [110.0, 109.0, 110.0]

    result = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=(structure_pivot(candles),)
    )

    assert result.evidence.pullback_touch is True
    assert TriggerReason.NO_MA_PULLBACK_TOUCH not in result.reason_codes


def test_invalid_episode_is_auditable_failure() -> None:
    candles, indicators = trigger_a_inputs()
    context = setup_context().model_copy(update={"eligible_for_trigger": False})

    result = evaluate_trigger_a(
        candles, indicators, setup=context, pivots=(structure_pivot(candles),)
    )

    assert result.reason_codes == (TriggerReason.NO_VALID_EPISODE,)


def test_trigger_a_history_and_grid_fail_closed() -> None:
    candles, indicators = trigger_a_inputs()
    with pytest.raises(TriggerNotReadyError, match="24"):
        evaluate_trigger_a(
            candles.iloc[:23],
            indicators.iloc[:23],
            setup=setup_context(),
            pivots=(),
        )

    candles.loc[10:, "open_time"] += pd.Timedelta(minutes=15)
    candles.loc[10:, "close_time_exclusive"] += pd.Timedelta(minutes=15)
    with pytest.raises(TriggerInputError, match="complete"):
        evaluate_trigger_a(candles, indicators, setup=setup_context(), pivots=())


def test_trigger_a_is_repeatable_and_future_pivots_do_not_change_history() -> None:
    candles, indicators = trigger_a_inputs()
    pivot = structure_pivot(candles)

    first = evaluate_trigger_a(candles, indicators, setup=setup_context(), pivots=(pivot,))
    repeated = evaluate_trigger_a(candles, indicators, setup=setup_context(), pivots=(pivot,))

    assert first == repeated
    assert first.model_dump_json() == repeated.model_dump_json()


def test_gs029_trigger_b_long_and_short_mirror_confirm() -> None:
    candles, indicators = trigger_b_inputs()
    long_result = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(sweep_zone(candles),)
    )

    short_candles, short_indicators = mirror_prices(candles, indicators)
    short_result = evaluate_trigger_b(
        short_candles,
        short_indicators,
        setup=setup_context(Direction.SHORT),
        zones=(sweep_zone(candles, PivotType.HIGH),),
    )

    assert long_result.confirmed is True
    assert short_result.confirmed is True
    assert long_result.reason_codes == (TriggerReason.TRIGGER_B_CONFIRMED,)
    assert short_result.reason_codes == (TriggerReason.TRIGGER_B_CONFIRMED,)
    assert long_result.invalidation_price == 99.0
    assert short_result.invalidation_price == 101.0


def test_gs030_reclaim_must_clear_the_complete_zone() -> None:
    candles, indicators = trigger_b_inputs()
    candles.loc[96, "close"] = 101.0

    result = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(sweep_zone(candles),)
    )

    assert TriggerReason.SWEEP_RECLAIM_FAILED in result.reason_codes
    assert result.evidence.zone_reclaimed is False


def test_gs031_destroyed_zone_is_missing_and_equal_boundary_survives() -> None:
    candles, indicators = trigger_b_inputs()
    zone = sweep_zone(candles)
    candles.loc[50, ["high", "low", "close"]] = [101.0, 99.0, 99.9]
    destroyed = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(zone,)
    )
    assert TriggerReason.SWING_ZONE_MISSING in destroyed.reason_codes
    assert destroyed.evidence.selected_zone_id is None

    candles.loc[50, ["high", "low", "close"]] = [101.0, 100.0, 100.0]
    boundary = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(zone,)
    )
    assert boundary.evidence.selected_zone_id == zone.zone_id


def test_sweep_and_ma_reclaim_are_strict_at_equality() -> None:
    candles, indicators = trigger_b_inputs()
    zone = sweep_zone(candles)
    candles.loc[96, "low"] = 100.0
    equal_sweep = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(zone,)
    )
    assert TriggerReason.SWEEP_NOT_BEYOND_ZONE in equal_sweep.reason_codes

    candles.loc[96, "low"] = 99.0
    indicators.loc[96, "sma30"] = 111.0
    equal_sma = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(zone,)
    )
    assert TriggerReason.MA_RECLAIM_FAILED in equal_sma.reason_codes


def test_trigger_b_does_not_require_pullback_volume_contraction() -> None:
    candles, _ = trigger_b_inputs()
    candles.loc[93:95, "quote_volume"] = 2_000.0
    candles.loc[96, "quote_volume"] = 2_500.0
    indicators = pd.DataFrame({"sma30": np.full(97, 110.0)})
    indicators = pd.concat(
        [indicators, quote_volume_statistics(candles["quote_volume"])], axis=1
    )

    result = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(sweep_zone(candles),)
    )

    assert result.evidence.volume.pullback_contracting is False
    assert result.confirmed is True


def test_zone_must_exist_before_sweep_open_and_inside_96_bar_window() -> None:
    candles, indicators = trigger_b_inputs()
    future_zone = sweep_zone(candles, confirmed_position=96)
    original_old_zone = sweep_zone(candles)
    old_members = tuple(
        PivotEvent.model_validate(
            {
                **member.model_dump(),
                "pivot_time": pd.Timestamp(candles["open_time"].iloc[0]).to_pydatetime()
                - timedelta(minutes=15),
            }
        )
        for member in original_old_zone.members
    )
    old_zone = PivotZone(
        zone_id=original_old_zone.zone_id,
        interval=original_old_zone.interval,
        kind=original_old_zone.kind,
        lower=original_old_zone.lower,
        upper=original_old_zone.upper,
        confirmed_at=old_members[-1].confirmed_at,
        members=old_members,
    )

    future = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=(future_zone,)
    )
    old = evaluate_trigger_b(candles, indicators, setup=setup_context(), zones=(old_zone,))

    assert future.reason_codes[0] is TriggerReason.SWING_ZONE_MISSING
    assert old.reason_codes[0] is TriggerReason.SWING_ZONE_MISSING


def test_zone_selection_prioritizes_latest_confirmation_before_distance() -> None:
    candles, indicators = trigger_b_inputs()
    older_near = sweep_zone(candles, confirmed_position=22, zone_id_character="1")
    newer_far = sweep_zone(candles, confirmed_position=32, zone_id_character="2")

    result = evaluate_trigger_b(
        candles,
        indicators,
        setup=setup_context(),
        zones=(older_near, newer_far),
    )

    assert result.evidence.selected_zone_id == newer_far.zone_id


def test_trigger_b_requires_complete_96_bar_history() -> None:
    candles, indicators = trigger_b_inputs()

    with pytest.raises(TriggerNotReadyError, match="97"):
        evaluate_trigger_b(
            candles.iloc[1:],
            indicators.iloc[1:],
            setup=setup_context(),
            zones=(),
        )


def test_trigger_b_is_repeatable() -> None:
    candles, indicators = trigger_b_inputs()
    zone = sweep_zone(candles)

    first = evaluate_trigger_b(candles, indicators, setup=setup_context(), zones=(zone,))
    repeated = evaluate_trigger_b(candles, indicators, setup=setup_context(), zones=(zone,))

    assert first == repeated
    assert first.model_dump_json() == repeated.model_dump_json()


def trigger_pair() -> tuple[TriggerAEvaluation, TriggerBEvaluation]:
    candles, indicators = trigger_b_inputs()
    trigger_a = evaluate_trigger_a(
        candles,
        indicators,
        setup=setup_context(),
        pivots=(structure_pivot(candles),),
    )
    trigger_b = evaluate_trigger_b(
        candles,
        indicators,
        setup=setup_context(),
        zones=(sweep_zone(candles),),
    )
    return trigger_a, trigger_b


def test_gs032_a_and_b_merge_to_one_sweep_primary_signal() -> None:
    trigger_a, trigger_b = trigger_pair()

    decision = merge_trigger_evaluations(trigger_a, trigger_b)

    assert decision.primary_trigger is TriggerType.SWEEP_RECLAIM
    assert decision.all_triggers == (
        TriggerType.SWEEP_RECLAIM,
        TriggerType.MA_RECLAIM,
    )
    assert decision.logical_signal_id is not None
    assert decision.eligible_for_plan is True


def test_merge_truth_table_and_signal_id_are_deterministic() -> None:
    candles, indicators = trigger_b_inputs()
    confirmed_a = evaluate_trigger_a(
        candles,
        indicators,
        setup=setup_context(),
        pivots=(structure_pivot(candles),),
    )
    failed_a = evaluate_trigger_a(
        candles, indicators, setup=setup_context(), pivots=()
    )
    confirmed_b = evaluate_trigger_b(
        candles,
        indicators,
        setup=setup_context(),
        zones=(sweep_zone(candles),),
    )
    failed_b = evaluate_trigger_b(
        candles, indicators, setup=setup_context(), zones=()
    )

    both = merge_trigger_evaluations(confirmed_a, confirmed_b)
    a_only = merge_trigger_evaluations(confirmed_a, failed_b)
    b_only = merge_trigger_evaluations(failed_a, confirmed_b)
    neither = merge_trigger_evaluations(failed_a, failed_b)

    assert a_only.primary_trigger is TriggerType.MA_RECLAIM
    assert a_only.all_triggers == (TriggerType.MA_RECLAIM,)
    assert b_only.primary_trigger is TriggerType.SWEEP_RECLAIM
    assert b_only.all_triggers == (TriggerType.SWEEP_RECLAIM,)
    assert neither.primary_trigger is None
    assert neither.all_triggers == ()
    assert neither.logical_signal_id is None
    assert both.logical_signal_id == a_only.logical_signal_id == b_only.logical_signal_id

    timestamp = both.confirmation_open_time.isoformat(timespec="milliseconds")
    expected = hashlib.sha256(
        "|".join(
            (
                "trigger/0.1.0",
                "ma-trend-pullback/0.1.0",
                "BTCUSDT",
                "LONG",
                "a" * 64,
                timestamp,
            )
        ).encode()
    ).hexdigest()
    assert both.logical_signal_id == expected
    assert both == merge_trigger_evaluations(confirmed_a, confirmed_b)
    assert both.model_dump_json() == merge_trigger_evaluations(
        confirmed_a, confirmed_b
    ).model_dump_json()


def test_trigger_b_not_ready_does_not_erase_confirmed_a() -> None:
    candles, indicators = trigger_a_inputs()

    decision = evaluate_triggers(
        candles,
        indicators,
        setup=setup_context(),
        pivots=(structure_pivot(candles),),
        zones=(),
    )

    assert decision.trigger_a.confirmed is True
    assert decision.trigger_b is None
    assert decision.primary_trigger is TriggerType.MA_RECLAIM
    assert decision.logical_signal_id is not None


def test_merge_rejects_different_setup_or_confirmation_candle() -> None:
    candles, indicators = trigger_b_inputs()
    trigger_a = evaluate_trigger_a(
        candles,
        indicators,
        setup=setup_context(),
        pivots=(structure_pivot(candles),),
    )
    trigger_b = evaluate_trigger_b(
        candles,
        indicators,
        setup=setup_context(),
        zones=(sweep_zone(candles),),
    )
    mismatch = trigger_b.model_copy(update={"episode_id": "f" * 64})

    with pytest.raises(TriggerInputError, match="same Setup"):
        merge_trigger_evaluations(trigger_a, mismatch)
