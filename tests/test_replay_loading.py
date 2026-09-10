"""Joint loading and P7 integration using explicitly synthetic plans, rules and REST bodies."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from slagalpha.backtest.analytics import apply_funding
from slagalpha.backtest.costs import CostScenario, replay_with_costs
from slagalpha.backtest.replay import TradeExitReason
from slagalpha.domain.universe import ContractRegistry, EvidenceConfidence, RegistryVerification
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    ReplayDataInputError,
    build_dev_replay_data_request,
)
from slagalpha.research.replay_loading import load_bound_replay_inputs
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    build_replay_market_data_artifact,
)
from test_replay_inputs import _context
from test_replay_market_data import _funding, _klines, _response


def _fixture(
    root: Path, *, late_entry: bool = False,
) -> tuple[
    dict[str, Any], DevReplayDataRequest, ReplayMarketDataArtifact, ReplayMarketDataArtifact,
]:
    context = _context()
    request = build_dev_replay_data_request(**context)
    rows = _klines(request)
    ttl_minutes = int((request.request.armed.expires_at - request.start).total_seconds() / 60)
    last_entry_index = ttl_minutes - 1
    for index, row in enumerate(rows):
        prices = ["112.2", "113", "111", "112.2"]
        if late_entry and index < last_entry_index:
            prices = ["110", "111", "109", "110"]
        if not late_entry and index == 180:
            prices[1] = "145"
        row[1:5] = prices
    minute_response = _response(root, request, rows, name="minutes.json")
    funding_response = _response(
        root, request, _funding(request), name="funding.json", endpoint="/fapi/v1/fundingRate",
    )
    minute_artifact, _ = build_replay_market_data_artifact(
        project_dir=root, request=request, role="CANDLE_ONE_MINUTE", responses=(minute_response,),
    )
    funding_artifact, _ = build_replay_market_data_artifact(
        project_dir=root, request=request, role="FUNDING", responses=(funding_response,),
    )
    return context, request, minute_artifact, funding_artifact


@pytest.mark.parametrize("late_entry", [False, True])
def test_loaded_synthetic_inputs_close_in_existing_p7_and_apply_funding(
    tmp_path: Path, late_entry: bool,
) -> None:
    context, request, one_minute, funding = _fixture(tmp_path, late_entry=late_entry)
    candles, dataset = load_bound_replay_inputs(
        project_dir=tmp_path, request=request, one_minute=one_minute, funding=funding, **context,
    )
    # Replay is explicitly confined to synthetic test fixtures, not called by the loader.
    replay = replay_with_costs(request.request, candles, CostScenario.BASELINE)
    result = apply_funding(replay, dataset)
    assert result.net_statistics_eligible is True
    assert result.funding_data_missing is False
    assert result.funding_cash_flow == Decimal("0.01")
    if late_entry:
        assert replay.trade.exit_reason is TradeExitReason.TIME_EXIT
        assert replay.trade.exit_time == request.end_exclusive - timedelta(minutes=1)
    else:
        assert replay.trade.exit_reason is TradeExitReason.TP2


def test_unverified_upstream_rules_block_before_any_data_file_read(tmp_path: Path) -> None:
    context, request, one_minute, funding = _fixture(tmp_path)
    rule = context["registry"].entries[0].model_copy(update={
        "verification_status": RegistryVerification.UNVERIFIED,
        "confidence": EvidenceConfidence.LOW,
    })
    context["registry"] = ContractRegistry(registry_version="synthetic-rules", entries=(rule,))
    with pytest.raises(ReplayDataInputError, match="usable DEV historical rule"):
        load_bound_replay_inputs(
            project_dir=tmp_path / "nonexistent", request=request,
            one_minute=one_minute, funding=funding, **context,
        )


def test_joint_loader_rejects_swapped_roles_and_cross_request_artifacts(tmp_path: Path) -> None:
    context, request, one_minute, funding = _fixture(tmp_path)
    with pytest.raises(ReplayDataInputError, match="one 1m and one Funding"):
        load_bound_replay_inputs(
            project_dir=tmp_path / "nonexistent", request=request,
            one_minute=funding, funding=one_minute, **context,
        )
    mismatched = funding.model_copy(update={"request_hash": "a" * 64})
    with pytest.raises(ReplayDataInputError, match="same request"):
        load_bound_replay_inputs(
            project_dir=tmp_path / "nonexistent", request=request,
            one_minute=one_minute, funding=mismatched, **context,
        )


def test_joint_loader_rechecks_raw_funding_after_successful_load(tmp_path: Path) -> None:
    context, request, one_minute, funding = _fixture(tmp_path)
    load_bound_replay_inputs(
        project_dir=tmp_path, request=request, one_minute=one_minute, funding=funding, **context,
    )
    (tmp_path / "funding.json").write_bytes(b"[]")
    with pytest.raises(ReplayDataInputError, match="file hash mismatch"):
        load_bound_replay_inputs(
            project_dir=tmp_path, request=request,
            one_minute=one_minute, funding=funding, **context,
        )
