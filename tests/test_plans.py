"""Golden and boundary tests for Decimal Entry/Stop planning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from slagalpha.strategy.pivots import PivotEvent, PivotType, PivotZone
from slagalpha.strategy.plans import (
    EntryStopEvaluation,
    EntryStopRequest,
    OneHourClosePosition,
    PlanInputError,
    PlanReason,
    RoundingDirection,
    ScoreInput,
    TakeProfitEvaluation,
    TargetSource,
    build_entry_stop,
    build_take_profit,
    ceil_to_tick,
    floor_to_tick,
    score_trade_plan,
)
from slagalpha.strategy.setup import DailyContext, Direction, PullbackState
from slagalpha.strategy.triggers import TriggerType


def entry_stop_request(
    *,
    direction: Direction = Direction.LONG,
    confirmation_high: str = "112.03",
    confirmation_low: str = "100",
    invalidation_price: str = "99.97",
    atr: str = "10",
    tick: str = "0.1",
) -> EntryStopRequest:
    return EntryStopRequest.create(
        logical_signal_id="a" * 64,
        symbol="btcusdt",
        direction=direction,
        primary_trigger=TriggerType.MA_RECLAIM,
        confirmation_high=confirmation_high,
        confirmation_low=confirmation_low,
        invalidation_price=invalidation_price,
        atr_at_confirmation=atr,
        tick_size=tick,
        confirmation_close=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_gs033_long_and_short_round_conservatively() -> None:
    long_result = build_entry_stop(entry_stop_request())
    short_result = build_entry_stop(
        entry_stop_request(
            direction=Direction.SHORT,
            confirmation_high="100",
            confirmation_low="87.97",
            invalidation_price="100.03",
        )
    )

    assert long_result.accepted is True
    assert long_result.raw_entry == Decimal("112.13")
    assert long_result.entry_price == Decimal("112.2")
    assert long_result.entry_rounding is RoundingDirection.CEILING
    assert long_result.stop_buffer == Decimal("1.50")
    assert long_result.raw_stop == Decimal("98.47")
    assert long_result.stop_price == Decimal("98.4")
    assert long_result.stop_rounding is RoundingDirection.FLOOR

    assert short_result.accepted is True
    assert short_result.entry_price == Decimal("87.8")
    assert short_result.stop_price == Decimal("101.6")
    assert short_result.risk_per_unit == long_result.risk_per_unit


def test_non_decimal_tick_grid_uses_integer_units() -> None:
    assert ceil_to_tick(Decimal("100.01"), Decimal("0.25")) == Decimal("100.25")
    assert floor_to_tick(Decimal("100.24"), Decimal("0.25")) == Decimal("100.00")


@pytest.mark.parametrize(
    ("invalidation", "expected_risk", "accepted"),
    [
        ("96.4", Decimal("5.0"), True),
        ("81.4", Decimal("20.0"), True),
        ("81.3", Decimal("20.1"), False),
    ],
)
def test_gs034_stop_distance_boundaries(
    invalidation: str,
    expected_risk: Decimal,
    accepted: bool,
) -> None:
    request = entry_stop_request(
        confirmation_high="99.8",
        confirmation_low="95",
        invalidation_price=invalidation,
    )

    result = build_entry_stop(request)

    assert result.risk_per_unit == expected_risk
    assert result.accepted is accepted
    assert (PlanReason.STOP_DISTANCE_INVALID in result.reason_codes) is not accepted


@pytest.mark.parametrize(
    ("tick", "atr", "reason"),
    [
        ("0", "10", PlanReason.TICK_SIZE_INVALID),
        ("0.1", "0", PlanReason.ATR_INVALID),
        ("NaN", "10", PlanReason.TICK_SIZE_INVALID),
    ],
)
def test_invalid_tick_and_atr_return_rejected_evidence(
    tick: str,
    atr: str,
    reason: PlanReason,
) -> None:
    result = build_entry_stop(entry_stop_request(tick=tick, atr=atr))

    assert result.accepted is False
    assert reason in result.reason_codes
    assert result.entry_price is None


def test_stop_on_wrong_side_is_rejected() -> None:
    request = entry_stop_request(invalidation_price="120")

    result = build_entry_stop(request)

    assert result.accepted is False
    assert PlanReason.STOP_SIDE_INVALID in result.reason_codes


def test_ttl_is_right_open_and_only_frozen_values_are_allowed() -> None:
    request = entry_stop_request()
    result = build_entry_stop(request, entry_ttl_bars=6)

    assert result.expires_at == request.confirmation_close + timedelta(minutes=90)
    with pytest.raises(PlanInputError, match="2, 4, or 6"):
        build_entry_stop(request, entry_ttl_bars=5)
    with pytest.raises(PlanInputError, match="0.10"):
        build_entry_stop(request, stop_atr_multiplier="0.12")


def test_float_inputs_use_decimal_string_conversion_and_repeat_exactly() -> None:
    request = EntryStopRequest.create(
        logical_signal_id="a" * 64,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        primary_trigger=TriggerType.MA_RECLAIM,
        confirmation_high=112.03,
        confirmation_low=100.0,
        invalidation_price=99.97,
        atr_at_confirmation=10.0,
        tick_size=0.1,
        confirmation_close=datetime(2024, 1, 1, tzinfo=UTC),
    )

    first = build_entry_stop(request)
    repeated = build_entry_stop(request)

    assert request.confirmation_high == Decimal("112.03")
    assert request.tick_size == Decimal("0.1")
    assert first == repeated
    assert first.model_dump_json() == repeated.model_dump_json()


def target_frames(confirmation_close: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    fifteen_opens = [
        confirmation_close - timedelta(minutes=15 * offset) for offset in range(96, 0, -1)
    ]
    one_hour_opens = [
        confirmation_close - timedelta(hours=offset) for offset in range(60, 0, -1)
    ]

    def frame(opens: list[datetime], duration: timedelta) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open_time": pd.to_datetime(opens, utc=True),
                "close_time_exclusive": pd.to_datetime(
                    [value + duration for value in opens], utc=True
                ),
                "is_closed": True,
            }
        )

    return frame(fifteen_opens, timedelta(minutes=15)), frame(
        one_hour_opens, timedelta(hours=1)
    )


def target_zone(
    *,
    interval: str,
    kind: PivotType,
    price: float,
    confirmation_close: datetime,
    zone_id_character: str,
    future: bool = False,
) -> PivotZone:
    duration = timedelta(minutes=15) if interval == "15m" else timedelta(hours=1)
    confirmed_at = confirmation_close + duration if future else confirmation_close - duration
    pivot_time = confirmed_at - 2 * duration
    pivot = PivotEvent(
        pivot_id=zone_id_character * 64,
        interval=interval,
        kind=kind,
        pivot_time=pivot_time,
        confirmed_at=confirmed_at,
        center_position=1,
        confirmed_position=3,
        price=price,
        atr_at_confirmation=10.0,
        left=2,
        right=2,
    )
    return PivotZone(
        zone_id=zone_id_character * 64,
        interval=interval,
        kind=kind,
        lower=price,
        upper=price,
        confirmed_at=confirmed_at,
        members=(pivot,),
    )


def accepted_entry_stop(
    *,
    direction: Direction = Direction.LONG,
    risk: str = "10",
) -> EntryStopEvaluation:
    if direction is Direction.LONG:
        confirmation_high = "99.9"
        confirmation_low = "95"
        invalidation = str(Decimal("100") - Decimal(risk) + Decimal("1.5"))
    else:
        confirmation_high = "105"
        confirmation_low = "100.1"
        invalidation = str(Decimal("100") + Decimal(risk) - Decimal("1.5"))
    result = build_entry_stop(
        entry_stop_request(
            direction=direction,
            confirmation_high=confirmation_high,
            confirmation_low=confirmation_low,
            invalidation_price=invalidation,
        )
    )
    assert result.accepted
    return result


def test_gs035_obstacle_below_one_r_rejects_and_equal_one_r_passes() -> None:
    entry_stop = accepted_entry_stop()
    fifteen, one_hour = target_frames(entry_stop.request.confirmation_close)
    obstacle = target_zone(
        interval="15m",
        kind=PivotType.HIGH,
        price=110.0,
        confirmation_close=entry_stop.request.confirmation_close,
        zone_id_character="b",
    )

    rejected = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
        fifteen_minute_zones=(obstacle,),
    )
    assert rejected.reason_codes == (PlanReason.OBSTACLE_LT_1R,)
    assert rejected.candidates[0].target_price == Decimal("109.9")
    assert rejected.candidates[0].gross_rr == Decimal("0.99")

    exact = target_zone(
        interval="15m",
        kind=PivotType.HIGH,
        price=110.1,
        confirmation_close=entry_stop.request.confirmation_close,
        zone_id_character="c",
    )
    accepted = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
        fifteen_minute_zones=(exact,),
    )
    assert accepted.accepted is True
    assert accepted.tp1 == Decimal("110.0")
    assert accepted.gross_rr_tp1 == Decimal("1.0")


def test_gs036_atr_extensions_and_short_mirror() -> None:
    long_entry_stop = accepted_entry_stop(risk="8")
    fifteen, one_hour = target_frames(long_entry_stop.request.confirmation_close)
    long_plan = build_take_profit(
        long_entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
    )
    short_entry_stop = accepted_entry_stop(direction=Direction.SHORT, risk="8")
    short_plan = build_take_profit(
        short_entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
    )

    assert long_plan.tp1 == Decimal("110.0")
    assert long_plan.tp2 == Decimal("120.0")
    assert short_plan.tp1 == Decimal("90.0")
    assert short_plan.tp2 == Decimal("80.0")
    assert long_plan.tp1_source is TargetSource.ATR_EXTENSION
    assert long_plan.tp2_source is TargetSource.ATR_EXTENSION


def test_distinct_structure_zones_supply_tp1_and_tp2() -> None:
    entry_stop = accepted_entry_stop()
    fifteen, one_hour = target_frames(entry_stop.request.confirmation_close)
    tp1_zone = target_zone(
        interval="15m",
        kind=PivotType.HIGH,
        price=110.1,
        confirmation_close=entry_stop.request.confirmation_close,
        zone_id_character="d",
    )
    tp2_zone = target_zone(
        interval="1h",
        kind=PivotType.HIGH,
        price=120.1,
        confirmation_close=entry_stop.request.confirmation_close,
        zone_id_character="e",
    )

    plan = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
        fifteen_minute_zones=(tp1_zone,),
        one_hour_zones=(tp2_zone,),
    )

    assert plan.accepted is True
    assert plan.tp1_source is TargetSource.STRUCTURE_15M
    assert plan.tp1_zone_id == tp1_zone.zone_id
    assert plan.tp2_source is TargetSource.STRUCTURE_1H
    assert plan.tp2_zone_id == tp2_zone.zone_id
    assert plan.gross_rr_tp2 == Decimal("2.0")


def test_future_zone_is_ignored_and_far_tp1_without_farther_tp2_fails() -> None:
    entry_stop = accepted_entry_stop()
    fifteen, one_hour = target_frames(entry_stop.request.confirmation_close)
    future_zone = target_zone(
        interval="15m",
        kind=PivotType.HIGH,
        price=105.0,
        confirmation_close=entry_stop.request.confirmation_close,
        zone_id_character="f",
        future=True,
    )
    ignored = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
        fifteen_minute_zones=(future_zone,),
    )
    assert ignored.accepted is True
    assert ignored.candidates == ()

    far_tp1 = target_zone(
        interval="15m",
        kind=PivotType.HIGH,
        price=130.1,
        confirmation_close=entry_stop.request.confirmation_close,
        zone_id_character="a",
    )
    invalid = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
        fifteen_minute_zones=(far_tp1,),
    )
    assert invalid.accepted is False
    assert invalid.reason_codes == (PlanReason.TP_PLAN_INVALID,)


def test_take_profit_is_repeatable() -> None:
    entry_stop = accepted_entry_stop()
    fifteen, one_hour = target_frames(entry_stop.request.confirmation_close)

    first = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
    )
    repeated = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
    )

    assert first == repeated
    assert first.model_dump_json() == repeated.model_dump_json()


def maximum_score_plan(direction: Direction = Direction.LONG) -> TakeProfitEvaluation:
    entry_stop = accepted_entry_stop(direction=direction)
    fifteen, one_hour = target_frames(entry_stop.request.confirmation_close)
    if direction is Direction.LONG:
        prices = (110.1, 130.1)
        kind = PivotType.HIGH
    else:
        prices = (89.9, 69.9)
        kind = PivotType.LOW
    zones = tuple(
        target_zone(
            interval="15m",
            kind=kind,
            price=price,
            confirmation_close=entry_stop.request.confirmation_close,
            zone_id_character=character,
        )
        for price, character in zip(prices, ("1", "2"), strict=True)
    )
    plan = build_take_profit(
        entry_stop,
        fifteen_minute_candles=fifteen,
        one_hour_candles=one_hour,
        fifteen_minute_zones=zones,
    )
    assert plan.accepted
    return plan


def maximum_score_input(direction: Direction = Direction.LONG) -> ScoreInput:
    return ScoreInput(
        direction=direction,
        four_hour_direction_valid=True,
        band_width=Decimal("1.50"),
        normalized_slope=Decimal("0.50"),
        daily_context=DailyContext.ALIGNED,
        pullback_state=PullbackState.SHALLOW,
        one_hour_close_position=OneHourClosePosition.TREND_SIDE_SMA30,
        one_hour_volume_contracting=True,
        primary_trigger=TriggerType.MA_RECLAIM,
        trigger_pullback_contracting=True,
        sweep_depth_atr=None,
        sweep_zone_reclaimed=False,
        trigger_volume_ratio=Decimal("1.50"),
        trigger_volume_expanding=True,
        close_location=Decimal("0.80") if direction is Direction.LONG else Decimal("0.20"),
        strict_structure_reclaim=True,
        take_profit=maximum_score_plan(direction),
    )


def test_score_maximum_is_100_and_short_mirror_matches() -> None:
    evidence = maximum_score_input()
    long_score = score_trade_plan(evidence)
    short_score = score_trade_plan(maximum_score_input(Direction.SHORT))

    assert long_score.score_total == 100
    assert long_score == short_score
    assert (long_score.four_hour, long_score.daily, long_score.one_hour) == (25, 15, 25)
    assert (long_score.fifteen_minute, long_score.risk_reward) == (20, 15)
    assert long_score.model_dump_json() == score_trade_plan(evidence).model_dump_json()


def test_score_threshold_boundaries_are_exact_and_have_no_rejection_cutoff() -> None:
    evidence = maximum_score_input().model_copy(
        update={
            "band_width": Decimal("1.00"),
            "normalized_slope": Decimal("0.25"),
            "daily_context": DailyContext.MIXED,
            "pullback_state": PullbackState.STANDARD,
            "one_hour_close_position": OneHourClosePosition.DEEP_VALID,
            "one_hour_volume_contracting": False,
            "trigger_pullback_contracting": False,
            "trigger_volume_ratio": Decimal("1.01"),
            "close_location": Decimal("0.65"),
        }
    )

    score = score_trade_plan(ScoreInput.model_validate(evidence.model_dump()))

    assert score.four_hour == 23
    assert score.daily == 7
    assert score.one_hour == 14
    assert score.fifteen_minute == 11
    assert score.score_total == sum(
        (score.four_hour, score.daily, score.one_hour, score.fifteen_minute, score.risk_reward)
    )
    assert 0 <= score.score_total <= 100


def test_score_fails_closed_on_mandatory_gate_contradiction() -> None:
    invalid = maximum_score_input().model_copy(
        update={"trigger_volume_expanding": False}
    )

    with pytest.raises(PlanInputError, match="mandatory"):
        score_trade_plan(ScoreInput.model_validate(invalid.model_dump()))
