"""Canonical, reproducible replay summaries for P7.4c2."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from slagalpha.backtest.analytics import ExcursionResult, FundingAdjustedResult
from slagalpha.backtest.replay import REPLAY_VERSION


class CanonicalReplaySummary(BaseModel):
    """Immutable digest plus the exact canonical JSON that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    replay_version: str = REPLAY_VERSION
    trade_id: str
    canonical_hash: str
    canonical_json: str


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _time_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_payload(
    adjusted: FundingAdjustedResult,
    excursions: ExcursionResult | None,
) -> dict[str, Any]:
    costed = adjusted.costed
    trade = costed.trade
    request = trade.request
    armed = trade.armed_result
    return {
        "replay_version": REPLAY_VERSION,
        "plan_version": request.plan_version,
        "logical_signal_id": request.armed.logical_signal_id,
        "symbol": request.armed.symbol,
        "direction": request.armed.direction.value,
        "scenario": costed.scenario.value,
        "rates": {
            "fee": _decimal_text(costed.rates.fee_rate),
            "slippage": _decimal_text(costed.rates.slippage_rate),
        },
        "plan": {
            "entry": _decimal_text(request.armed.entry_price),
            "stop": _decimal_text(request.armed.stop_price),
            "tp1": _decimal_text(request.tp1),
            "tp2": _decimal_text(request.tp2),
            "tick_size": _decimal_text(request.tick_size),
            "max_holding_bars": request.max_holding_bars,
        },
        "outcome": {
            "armed_state": armed.state.value,
            "position_state": (
                trade.position_state.value if trade.position_state is not None else None
            ),
            "exit_reason": trade.exit_reason.value if trade.exit_reason is not None else None,
            "entry_time": _time_text(armed.entry_time),
            "entry_price": _decimal_text(armed.theoretical_entry_price),
            "exit_time": _time_text(trade.exit_time),
            "breakeven": _decimal_text(trade.breakeven_price),
            "time_exit_deadline": _time_text(trade.time_exit_deadline),
            "ordering_source": trade.ordering_source.value,
            "ohlc_fallback_count": trade.ohlc_fallback_count,
        },
        "events": [
            {
                "id": event.event_id,
                "type": event.event_type.value,
                "time": _time_text(event.exchange_time),
                "source": event.source,
                "source_ref": event.source_ref,
                "reason": event.reason.value,
                "price": _decimal_text(event.theoretical_price),
                "fraction": _decimal_text(event.quantity_fraction),
            }
            for event in trade.events
        ],
        "fills": [
            {
                "event_id": fill.event_id,
                "type": fill.event_type.value,
                "side": fill.side.value,
                "fraction": _decimal_text(fill.quantity_fraction),
                "theoretical": _decimal_text(fill.theoretical_price),
                "simulated": _decimal_text(fill.simulated_price),
                "fee": _decimal_text(fill.fee),
                "slippage_cost": _decimal_text(fill.slippage_cost),
            }
            for fill in costed.fills
        ],
        "funding": {
            "applications": [
                {
                    "id": item.event_id,
                    "type": item.event_type.value,
                    "time": _time_text(item.settlement_time),
                    "rate": _decimal_text(item.rate),
                    "mark_price": _decimal_text(item.mark_price),
                    "open_fraction": _decimal_text(item.open_fraction),
                    "cash_flow": _decimal_text(item.cash_flow),
                }
                for item in adjusted.applications
            ],
            "cash_flow": _decimal_text(adjusted.funding_cash_flow),
            "data_missing": adjusted.funding_data_missing,
            "missing_times": [
                _time_text(value) for value in adjusted.missing_settlement_times
            ],
            "net_statistics_eligible": adjusted.net_statistics_eligible,
        },
        "metrics": {
            "gross_pnl": _decimal_text(costed.gross_pnl),
            "simulated_execution_pnl": _decimal_text(
                costed.simulated_execution_pnl
            ),
            "fee": _decimal_text(costed.total_fee),
            "slippage_cost": _decimal_text(costed.total_slippage_cost),
            "net_pnl": _decimal_text(adjusted.net_pnl),
            "gross_r": _decimal_text(costed.gross_r),
            "net_r": _decimal_text(adjusted.net_r),
            "mae_price": _decimal_text(
                excursions.mae_price if excursions is not None else None
            ),
            "mfe_price": _decimal_text(
                excursions.mfe_price if excursions is not None else None
            ),
            "mae_r": _decimal_text(
                excursions.mae_r if excursions is not None else None
            ),
            "mfe_r": _decimal_text(
                excursions.mfe_r if excursions is not None else None
            ),
        },
    }


def build_canonical_summary(
    adjusted: FundingAdjustedResult,
    excursions: ExcursionResult | None,
) -> CanonicalReplaySummary:
    """Hash the complete ordered replay output using normalized JSON."""

    event_ids = [event.event_id for event in adjusted.costed.trade.events]
    funding_ids = [application.event_id for application in adjusted.applications]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("replay event IDs must be unique before canonicalization")
    if len(set(funding_ids)) != len(funding_ids):
        raise ValueError("Funding event IDs must be unique before canonicalization")
    logical_signal_id = adjusted.costed.trade.request.armed.logical_signal_id
    trade_id = hashlib.sha256(
        f"{REPLAY_VERSION}|{logical_signal_id}".encode()
    ).hexdigest()
    canonical_json = json.dumps(
        _canonical_payload(adjusted, excursions),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    canonical_hash = hashlib.sha256(canonical_json.encode()).hexdigest()
    return CanonicalReplaySummary(
        trade_id=trade_id,
        canonical_hash=canonical_hash,
        canonical_json=canonical_json,
    )
