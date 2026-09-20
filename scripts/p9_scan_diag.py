"""Audit the 15 legacy RUNE scan hits without replay or new market data."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
from p9_dev_execution_inputs import PARAMETER_HASH, PLAN_HASH, ROOT
from p9_dev_source_scan import REGISTRY_HASH, SCAN_PLAN_HASH
from p9_normalization_gap_audit import UNIVERSE_RUN_VERSION

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.research.candle_history import (
    _required_months,
    last_closed_boundary,
    load_scan_candle_history,
)
from slagalpha.research.dev_source_scan import (
    _INTERVALS,
    _WARMUP_START,
    _aligned_stream_start,
    _available_structure,
    _IncrementalZoneState,
    _load_symbol_sources,
    _prepare_stream,
    _setup_at,
    _slice,
    _visible_end,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.scan_slot import compute_source_bound_scan_slot
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest
from slagalpha.strategy.setup import PullbackState
from slagalpha.strategy.triggers import TriggerSetupContext, evaluate_triggers

SYMBOL = "RUNEUSDT"
FIRST_LEGACY_SLOT = datetime(2025, 1, 29, 20, 15, tzinfo=UTC)
LEGACY_SLOT_COUNT = 15


def _load_context() -> tuple[
    SensitivityPlan,
    DevParameterVersion,
    ResearchSplitManifest,
    DevScanPlan,
    ContractRegistry,
    tuple[UniverseSnapshot, ...],
]:
    manifests = ROOT / "data/manifests"
    plan = SensitivityPlan.model_validate_json(
        (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    parameter = DevParameterVersion.model_validate_json(
        (manifests / "parameter_version" / f"{PARAMETER_HASH}.json").read_bytes()
    )
    split = ResearchSplitManifest.model_validate_json(
        (manifests / "research_split" / f"{plan.split_hash}.json").read_bytes()
    )
    scan_plan = DevScanPlan.model_validate_json(
        (manifests / "dev_scan_plan" / f"{SCAN_PLAN_HASH}.json").read_bytes()
    )
    registry = ContractRegistry.model_validate_json(
        (manifests / "contract_registry" / f"{REGISTRY_HASH}.json").read_bytes()
    )
    snapshots = []
    for progress_path in sorted(
        (manifests / "universe_batch_progress" / UNIVERSE_RUN_VERSION).glob("*.json")
    ):
        progress = json.loads(progress_path.read_bytes())
        snapshots.append(
            UniverseSnapshot.model_validate_json(
                (
                    manifests
                    / "universe_snapshot"
                    / f"{progress['universe_version']}.json"
                ).read_bytes()
            )
        )
    return plan, parameter, split, scan_plan, registry, tuple(snapshots)


def main() -> int:
    plan, parameter, split, scan_plan, registry, snapshots = _load_context()
    rule = next(item for item in registry.entries if item.symbol == SYMBOL)
    catalog = _load_symbol_sources(ROOT, SYMBOL, final_period="2025-01")

    # One full source-bound recomputation proves the independent P4/P5/P6 entry rejects
    # the legacy scanner's very first claimed hit before any batch-only diagnostics run.
    first_histories = []
    for interval in _INTERVALS:
        history_start = max(
            _WARMUP_START,
            _aligned_stream_start(rule.derived_first_candle_at, interval),
            catalog[interval][0].normalization.first_open_time,
        )
        periods = set(
            _required_months(
                history_start,
                last_closed_boundary(FIRST_LEGACY_SLOT, interval),
            )
        )
        _, history = load_scan_candle_history(
            project_dir=ROOT,
            scan_plan=scan_plan,
            symbol=SYMBOL,
            interval=interval,
            confirmation_close=FIRST_LEGACY_SLOT,
            history_start=history_start,
            sources=tuple(
                source for source in catalog[interval] if source.spec.period in periods
            ),
        )
        first_histories.append(history)
    source_bound = compute_source_bound_scan_slot(
        project_dir=ROOT,
        scan_plan=scan_plan,
        histories=tuple(first_histories),
        split=split,
        plan=plan,
        parameter=parameter,
        snapshots=snapshots,
        registry=registry,
    )
    print(
        json.dumps(
            {
                "check": "source_bound_first_slot",
                "slot": FIRST_LEGACY_SLOT,
                "status": source_bound.status,
                "eligible_for_plan": bool(
                    source_bound.trigger_evidence.decision
                    and source_bound.trigger_evidence.decision.eligible_for_plan
                ),
            },
            default=str,
            sort_keys=True,
        ),
        flush=True,
    )

    left, right = parameter.candidate.parameters.pivot_window
    streams = {
        interval: _prepare_stream(
            ROOT,
            catalog[interval],
            end_exclusive=scan_plan.days[-1].end_exclusive,
            lifecycle_start=rule.derived_first_candle_at,
            pivot_window=(left, right),
        )
        for interval in _INTERVALS
    }
    zone_states = {"15m": _IncrementalZoneState(), "1h": _IncrementalZoneState()}
    rows = []
    for index in range(LEGACY_SLOT_COUNT):
        at = FIRST_LEGACY_SLOT + timedelta(minutes=15 * index)
        ends = {
            interval: _visible_end(stream, at) for interval, stream in streams.items()
        }
        setup = _setup_at(
            streams, ends,
            compression_threshold=float(parameter.candidate.parameters.compression_threshold),
        )
        row: dict[str, object] = {
            "slot": at,
            "setup_reason": setup.decision_reason.value,
            "eligible_for_trigger": setup.eligible_for_trigger,
            "eligible_for_plan": False,
        }
        if setup.eligible_for_trigger:
            fifteen, fifteen_indicators = _slice(streams["15m"], ends["15m"], 97)
            hourly, hourly_indicators = _slice(streams["1h"], ends["1h"], 60)
            hourly_setup = setup.one_hour
            assert hourly_setup is not None
            assert hourly_setup.episode_id is not None
            assert hourly_setup.episode_start is not None
            fifteen_pivots, fifteen_zones = _available_structure(
                streams["15m"],
                at,
                zone_states["15m"],
                pivot_since=hourly_setup.episode_start,
                zone_since=pd.Timestamp(fifteen["open_time"].iloc[0]).to_pydatetime(),
            )
            _available_structure(
                streams["1h"],
                at,
                zone_states["1h"],
                pivot_since=hourly_setup.episode_start,
                zone_since=pd.Timestamp(hourly["open_time"].iloc[0]).to_pydatetime(),
            )
            columns = (
                ("sma30", "sma60")
                if hourly_setup.state is PullbackState.SHALLOW
                else ("sma60", "sma90")
            )
            bounds = sorted(
                float(hourly_indicators[column].iloc[-1]) for column in columns
            )
            decision = evaluate_triggers(
                fifteen,
                fifteen_indicators,
                setup=TriggerSetupContext(
                    symbol=SYMBOL,
                    direction=setup.four_hour.direction,
                    pullback_state=hourly_setup.state,
                    eligible_for_trigger=True,
                    episode_id=hourly_setup.episode_id,
                    episode_start=hourly_setup.episode_start,
                    region_lower=bounds[0],
                    region_upper=bounds[1],
                ),
                pivots=fifteen_pivots,
                zones=fifteen_zones,
            )
            row.update(
                {
                    "trigger_confirmation": decision.confirmation_close_time,
                    "eligible_for_plan": decision.eligible_for_plan,
                    "trigger_a_reasons": [
                        item.value for item in decision.trigger_a.reason_codes
                    ],
                }
            )
        rows.append(row)
        print(json.dumps(row, default=str, sort_keys=True), flush=True)
    if any(bool(row["eligible_for_plan"]) for row in rows):
        raise RuntimeError("a legacy RUNE hit survived exact-time recomputation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
