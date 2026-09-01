"""Idempotent replay event reducer for interruption recovery."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from slagalpha.backtest.replay import (
    PositionState,
    ReplayEvent,
    ReplayEventType,
    ReplayInputError,
    ReplayReason,
)


class LifecycleState(StrEnum):
    """Signal/trade state reconstructed only from the append-only event log."""

    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    INVALIDATED = "INVALIDATED"
    MISSED = "MISSED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


class ReducedReplayState(BaseModel):
    """Serializable checkpoint for deterministic resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lifecycle_state: LifecycleState = LifecycleState.ARMED
    position_state: PositionState | None = None
    open_fraction: Decimal = Decimal(0)
    breakeven_active: bool = False
    terminal_reason: ReplayReason | None = None
    closed_event_seen: bool = False
    applied_event_ids: tuple[str, ...] = ()
    last_event_time: datetime | None = None
    ignored_duplicate_count: int = 0


def reduce_event_log(
    events: tuple[ReplayEvent, ...],
    checkpoint: ReducedReplayState | None = None,
) -> ReducedReplayState:
    """Apply unseen events once; a returned checkpoint can resume after interruption."""

    state = checkpoint or ReducedReplayState()
    lifecycle = state.lifecycle_state
    position = state.position_state
    open_fraction = state.open_fraction
    breakeven_active = state.breakeven_active
    terminal_reason = state.terminal_reason
    closed_seen = state.closed_event_seen
    applied = list(state.applied_event_ids)
    applied_set = set(applied)
    last_time = state.last_event_time
    duplicates = state.ignored_duplicate_count

    for event in events:
        if event.event_id in applied_set:
            duplicates += 1
            continue
        if last_time is not None and event.exchange_time < last_time:
            raise ReplayInputError("event log must be ordered by exchange_time")

        if event.event_type is ReplayEventType.ENTRY_FILL:
            if lifecycle is not LifecycleState.ARMED or event.quantity_fraction != Decimal(1):
                raise ReplayInputError("ENTRY_FILL is invalid for reconstructed state")
            lifecycle = LifecycleState.TRIGGERED
            position = PositionState.FULL
            open_fraction = Decimal(1)
        elif event.event_type in {
            ReplayEventType.INVALIDATED,
            ReplayEventType.MISSED,
            ReplayEventType.EXPIRED,
        }:
            if lifecycle is not LifecycleState.ARMED:
                raise ReplayInputError("pre-trade terminal event is invalid after Entry")
            lifecycle = {
                ReplayEventType.INVALIDATED: LifecycleState.INVALIDATED,
                ReplayEventType.MISSED: LifecycleState.MISSED,
                ReplayEventType.EXPIRED: LifecycleState.EXPIRED,
            }[event.event_type]
            terminal_reason = event.reason
        elif event.event_type is ReplayEventType.TP1_FILL:
            if (
                lifecycle is not LifecycleState.TRIGGERED
                or position is not PositionState.FULL
                or event.quantity_fraction != Decimal("0.5")
            ):
                raise ReplayInputError("TP1_FILL is invalid for reconstructed state")
            position = PositionState.HALF_AFTER_TP1
            open_fraction = Decimal("0.5")
        elif event.event_type is ReplayEventType.BREAKEVEN_ACTIVATED:
            if (
                lifecycle is not LifecycleState.TRIGGERED
                or position is not PositionState.HALF_AFTER_TP1
            ):
                raise ReplayInputError("BREAKEVEN_ACTIVATED requires a half position")
            breakeven_active = True
        elif event.event_type in {
            ReplayEventType.STOP_FILL,
            ReplayEventType.TP2_FILL,
            ReplayEventType.TIME_EXIT,
        }:
            if lifecycle is not LifecycleState.TRIGGERED or position is None:
                raise ReplayInputError("terminal Fill requires a triggered position")
            if event.quantity_fraction != open_fraction:
                raise ReplayInputError("terminal Fill fraction differs from open position")
            lifecycle = LifecycleState.CLOSED
            position = PositionState.FLAT
            open_fraction = Decimal(0)
            terminal_reason = event.reason
        elif event.event_type is ReplayEventType.CLOSED:
            if lifecycle is not LifecycleState.CLOSED or closed_seen:
                raise ReplayInputError("CLOSED must follow exactly one terminal Fill")
            closed_seen = True
        elif event.event_type is ReplayEventType.LATE_EVENT_IGNORED:
            if lifecycle not in {
                LifecycleState.INVALIDATED,
                LifecycleState.MISSED,
                LifecycleState.EXPIRED,
            }:
                raise ReplayInputError("LATE_EVENT_IGNORED requires a pre-trade terminal state")
        elif event.event_type not in {
            ReplayEventType.FUNDING,
            ReplayEventType.FUNDING_DATA_MISSING,
        }:
            raise ReplayInputError(f"unsupported replay event: {event.event_type}")

        applied.append(event.event_id)
        applied_set.add(event.event_id)
        last_time = event.exchange_time

    return ReducedReplayState(
        lifecycle_state=lifecycle,
        position_state=position,
        open_fraction=open_fraction,
        breakeven_active=breakeven_active,
        terminal_reason=terminal_reason,
        closed_event_seen=closed_seen,
        applied_event_ids=tuple(applied),
        last_event_time=last_time,
        ignored_duplicate_count=duplicates,
    )
