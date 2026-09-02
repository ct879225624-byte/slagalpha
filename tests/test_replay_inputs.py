"""Synthetic accepted plans and VERIFIED fixtures exercise offline request boundaries only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    ReplayDataInputError,
    build_dev_replay_data_request,
    require_replay_data_request_binding,
    write_dev_replay_data_request,
)
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.strategy.plans import build_entry_stop, build_take_profit
from test_historical_replay import _rule
from test_plans import entry_stop_request, target_frames
from test_research_splits import _snapshot
from test_sensitivity_plan import _plan, _split


def _context(
    *, at: datetime = datetime(2024, 1, 2, 0, 15, tzinfo=UTC), candidate_index: int = 0,
) -> dict[str, Any]:
    plan = _plan(ready=True)
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[candidate_index].candidate_hash,
    )
    params = parameter.candidate.parameters
    seed = entry_stop_request().model_copy(update={"symbol": "AAAUSDT", "confirmation_close": at})
    entry_stop = build_entry_stop(
        seed, entry_ttl_bars=params.entry_ttl_bars, stop_atr_multiplier=params.stop_atr_multiplier,
    )
    fifteen, hourly = target_frames(at)
    return {
        "trade_plan": build_take_profit(
            entry_stop, fifteen_minute_candles=fifteen, one_hour_candles=hourly,
        ),
        "plan": plan, "parameter": parameter, "split": _split(),
        "universe": _snapshot(at.date()),
        "registry": ContractRegistry(
            registry_version="synthetic-rules", entries=(_rule(RegistryVerification.VERIFIED),),
        ),
    }


def test_default_request_includes_last_possible_time_exit_candle(tmp_path: Path) -> None:
    context = _context()
    request = build_dev_replay_data_request(**context)
    assert request.start == datetime(2024, 1, 2, 0, 15, tzinfo=UTC)
    assert request.end_exclusive == datetime(2024, 1, 2, 9, 1, tzinfo=UTC)
    assert request.expected_candle_count == 526
    assert request.download_authorized is request.research_authorized is False
    require_replay_data_request_binding(request, **context)
    path = write_dev_replay_data_request(request, tmp_path)
    assert write_dev_replay_data_request(request, tmp_path) == path
    assert DevReplayDataRequest.model_validate_json(path.read_bytes()) == request
    assert build_dev_replay_data_request(**context) == request


@pytest.mark.parametrize("candidate_index", range(10))
def test_every_candidate_uses_its_own_ttl_and_holding_horizon(candidate_index: int) -> None:
    context = _context(candidate_index=candidate_index)
    params = context["parameter"].candidate.parameters
    request = build_dev_replay_data_request(**context)
    expected = 15 * (params.entry_ttl_bars + params.max_holding_bars - 1) + 1
    assert request.expected_candle_count == expected


@pytest.mark.parametrize("at", [
    datetime(2024, 1, 2, 23, 45, tzinfo=UTC),  # DEV signal, but the data crosses VALIDATION.
    datetime(2024, 1, 3, 0, 15, tzinfo=UTC),  # VALIDATION.
    datetime(2024, 1, 4, 0, 15, tzinfo=UTC),  # LOCKED_TEST.
])
def test_entire_request_must_remain_inside_dev(at: datetime) -> None:
    with pytest.raises(ReplayDataInputError, match="inside DEV"):
        build_dev_replay_data_request(**_context(at=at))


@pytest.mark.parametrize("changes", [
    {"verification_status": RegistryVerification.UNVERIFIED},
    {"effective_to": datetime(2024, 1, 2, 0, 30, tzinfo=UTC)},
    {"tick_size": Decimal("0.01")},
    {"inferred_delisted_at": datetime(2024, 1, 2, 1, tzinfo=UTC)},
    {"status": "SETTLING"},
])
def test_rule_must_cover_full_horizon_with_matching_tick(changes: dict[str, Any]) -> None:
    context = _context()
    rule = context["registry"].entries[0].model_copy(update=changes)
    context["registry"] = ContractRegistry(registry_version="synthetic-rules", entries=(rule,))
    with pytest.raises(ReplayDataInputError):
        build_dev_replay_data_request(**context)


def test_rule_right_open_endpoint_accepts_exact_coverage_not_one_minute_less() -> None:
    context = _context()
    end = build_dev_replay_data_request(**context).end_exclusive
    rule = context["registry"].entries[0]
    context["registry"] = ContractRegistry(
        registry_version="synthetic-rules",
        entries=(rule.model_copy(update={"effective_to": end}),),
    )
    build_dev_replay_data_request(**context)
    context["registry"] = ContractRegistry(registry_version="synthetic-rules", entries=(
        rule.model_copy(update={"effective_to": end - timedelta(minutes=1)}),
    ))
    with pytest.raises(ReplayDataInputError, match="complete replay window"):
        build_dev_replay_data_request(**context)


def test_changed_candidate_trade_plan_and_universe_are_rejected() -> None:
    context = _context()
    context["parameter"] = build_dev_parameter_version(
        plan=context["plan"], candidate_hash=context["plan"].candidates[6].candidate_hash,
    )
    with pytest.raises(ReplayDataInputError, match="candidate Entry/Stop"):
        build_dev_replay_data_request(**context)
    context = _context()
    context["universe"] = _snapshot(date(2024, 1, 1))
    with pytest.raises(ReplayDataInputError, match="inactive"):
        build_dev_replay_data_request(**context)


def test_serialized_request_is_not_a_bypass_for_source_revalidation() -> None:
    context = _context()
    request = build_dev_replay_data_request(**context)
    payload = request.model_dump(mode="json")
    payload["end_exclusive"] = "2024-01-02T01:15:00Z"
    with pytest.raises(ValidationError, match="bounded replay window"):
        DevReplayDataRequest.model_validate(payload)
    rule = context["registry"].entries[0].model_copy(update={"source_ref": "fixture:changed"})
    context["registry"] = ContractRegistry(registry_version="synthetic-rules", entries=(rule,))
    with pytest.raises(ReplayDataInputError, match="current source evidence"):
        require_replay_data_request_binding(request, **context)


def test_real_frozen_blocked_plan_cannot_create_a_data_request() -> None:
    context = _context()
    context["plan"] = SensitivityPlan.model_validate_json(Path(
        "data/manifests/sensitivity_plan/"
        "c41e2771a8ca526e4c8ffa09a863b078e300fc7332b9090461e11e1c1f2b5ff9.json"
    ).read_bytes())
    context["parameter"] = build_dev_parameter_version(
        plan=context["plan"], candidate_hash=context["plan"].candidates[0].candidate_hash,
    )
    with pytest.raises(ReplayDataInputError, match="still blocked"):
        build_dev_replay_data_request(**context)
