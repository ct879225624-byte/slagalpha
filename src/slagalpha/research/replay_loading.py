"""Joint per-request input loading, without scheduling or executing a strategy replay."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from slagalpha.backtest.analytics import FundingDataset
from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    ReplayDataInputError,
    require_replay_data_request_binding,
)
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    load_replay_market_data_artifact,
)
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest
from slagalpha.strategy.plans import TakeProfitEvaluation


def load_bound_replay_inputs(
    *, project_dir: Path, request: DevReplayDataRequest,
    one_minute: ReplayMarketDataArtifact, funding: ReplayMarketDataArtifact,
    trade_plan: TakeProfitEvaluation, plan: SensitivityPlan, parameter: DevParameterVersion,
    split: ResearchSplitManifest, universe: UniverseSnapshot, registry: ContractRegistry,
) -> tuple[pd.DataFrame, FundingDataset]:
    """Recheck upstream evidence before reading files, then bind both datasets to one request."""
    require_replay_data_request_binding(
        request, trade_plan=trade_plan, plan=plan, parameter=parameter,
        split=split, universe=universe, registry=registry,
    )
    if one_minute.role != "CANDLE_ONE_MINUTE" or funding.role != "FUNDING":
        raise ReplayDataInputError("joint loader requires one 1m and one Funding artifact")
    if any(item.request_hash != request.request_hash for item in (one_minute, funding)):
        raise ReplayDataInputError("both market-data artifacts must belong to the same request")
    candles = load_replay_market_data_artifact(
        project_dir=project_dir, request=request, artifact=one_minute,
    )
    dataset = load_replay_market_data_artifact(
        project_dir=project_dir, request=request, artifact=funding,
    )
    if not isinstance(candles, pd.DataFrame) or not isinstance(dataset, FundingDataset):
        raise ReplayDataInputError("loaded market-data types do not match requested roles")
    return candles, dataset
