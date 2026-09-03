"""Opt-in complete synthetic DEV pipeline; no mocked validators, network, or real rule approval."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.data.archive import archive_path
from slagalpha.data.klines import INTERVAL_MILLISECONDS
from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.candle_history import (
    ScanInterval,
    last_closed_boundary,
    load_scan_candle_history,
)
from slagalpha.research.candle_inputs import CandleInputError, CandlePartitionSource
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.replay_market_data import build_replay_market_data_artifact
from slagalpha.research.request_set_market_data import DevRequestMarketDataPair
from slagalpha.research.scan_days import build_source_bound_scan_day
from slagalpha.research.scan_features import compute_scan_features
from slagalpha.research.scan_plan import build_dev_scan_plan
from slagalpha.research.scan_setup import compute_scan_setup
from slagalpha.research.scan_storage import save_source_bound_scan_day
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence, compute_scan_trade_plan
from slagalpha.research.scan_trigger import compute_scan_trigger
from slagalpha.research.sensitivity import build_default_sensitivity_plan
from slagalpha.research.source_request_set import (
    build_source_bound_replay_request_set,
    require_source_bound_market_data_report,
    verify_source_bound_request_set_market_data,
)
from slagalpha.research.splits import (
    audit_research_inputs,
    build_research_split,
    snapshot_sequence_hash,
)
from test_candle_inputs import _partition
from test_historical_replay import _rule
from test_replay_market_data import _funding, _klines, _response
from test_research_splits import _hash, _snapshot

TRIGGER_AT = datetime(2024, 1, 2, 12, 30, tzinfo=UTC)
LAST_CONFIRMATION = datetime(2024, 1, 2, 23, 45, tzinfo=UTC)
SeedInputs = dict[ScanInterval, tuple[datetime, tuple[CandlePartitionSource, ...]]]


def _prepare(root: Path) -> tuple[dict[str, Any], SeedInputs]:
    snapshots = []
    for index in range(4):
        snapshot = _snapshot(date(2024, 1, 1) + timedelta(days=index))
        members = (() if index == 0 else (
            snapshot.members[0].model_copy(update={"symbol": "BTCUSDT"}),
        ))
        snapshots.append(snapshot.model_copy(update={
            "members": members, "member_count": len(members),
            "universe_version": _hash(f"explicit-synthetic-source-pipeline:{index}:{len(members)}"),
        }))
    split = build_research_split(
        research_start=date(2024, 1, 1), research_end_exclusive=date(2024, 1, 5),
        universe_batch_run_version=_hash("explicit-synthetic-source-pipeline"),
        daily_snapshot_hash=snapshot_sequence_hash(tuple(snapshots)),
    )
    registry = ContractRegistry(registry_version="synthetic-rules", entries=(
        _rule(RegistryVerification.VERIFIED).model_copy(update={
            "symbol": "BTCUSDT", "base_asset": "BTC",
        }),
    ))
    plan = build_default_sensitivity_plan(
        split=split, audit=audit_research_inputs(split=split, snapshots=tuple(snapshots),
                                               registry=registry),
        strategy_rules_sha256=_hash("synthetic-strategy-reference-not-research"),
    )
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash,
    )
    context = {"project_dir": root, "split": split, "plan": plan, "parameter": parameter,
               "snapshots": tuple(snapshots), "registry": registry}
    context["scan_plan"] = build_dev_scan_plan(
        split=split, plan=plan, parameter=parameter, snapshots=tuple(snapshots),
    )
    seeds: SeedInputs = {}
    intervals: tuple[ScanInterval, ...] = ("15m", "1h", "4h", "1d")
    for interval, count in zip(intervals, (300, 240, 200, 192), strict=True):
        step = timedelta(milliseconds=INTERVAL_MILLISECONDS[interval])
        end = last_closed_boundary(LAST_CONFIRMATION, interval)
        start = end - count * step
        prices = [str(100 + index) for index in range(count)]
        overrides: dict[int, dict[str, str]] = {}
        if interval == "1h":
            event = int((last_closed_boundary(TRIGGER_AT, interval) - start) / step) - 1
            prices[event] = str(100 + event - 30)
        elif interval == "15m":
            event = int((TRIGGER_AT - start) / step) - 1
            prices = ["100"] * count
            prices[event - 5] = "99"
            prices[event - 4:event] = ["99.5"] * 4
            prices[event] = "103"
            overrides = {index: {"quote_volume": "800"} for index in range(event - 3, event)}
            overrides[event] = {"high": "103.2", "low": "99", "quote_volume": "1500"}
        sources = []
        cursor, position = start, 0
        while cursor < end:
            month = cursor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            next_month = (month.replace(year=month.year + 1, month=1) if month.month == 12
                          else month.replace(month=month.month + 1))
            piece_end = min(end, next_month)
            length = int((piece_end - cursor) / step)
            sources.append(_partition(
                root, start=cursor, interval=interval, rows=length,
                close_prices=tuple(prices[position:position + length]),
                row_overrides={index - position: value for index, value in overrides.items()
                               if position <= index < position + length},
            ))
            cursor, position = piece_end, position + length
        seeds[interval] = (start, tuple(sources))
    return context, seeds


def _source(context: dict[str, Any], seeds: SeedInputs, at: datetime) -> ScanTradePlanEvidence:
    features = []
    for interval, (start, sources) in seeds.items():
        _, history = load_scan_candle_history(
            project_dir=context["project_dir"], scan_plan=context["scan_plan"],
            symbol="BTCUSDT", interval=interval, confirmation_close=at,
            history_start=start, sources=sources,
        )
        _, _, feature = compute_scan_features(
            project_dir=context["project_dir"], scan_plan=context["scan_plan"],
            plan=context["plan"], parameter=context["parameter"], history=history,
        )
        features.append(feature)
    setup = compute_scan_setup(project_dir=context["project_dir"], scan_plan=context["scan_plan"],
                               plan=context["plan"], features=tuple(features))
    trigger = compute_scan_trigger(
        project_dir=context["project_dir"], scan_plan=context["scan_plan"],
        plan=context["plan"], setup_evidence=setup,
    )
    return compute_scan_trade_plan(
        project_dir=context["project_dir"], scan_plan=context["scan_plan"], plan=context["plan"],
        split=context["split"], registry=context["registry"], trigger_evidence=trigger,
        universe=next(item for item in context["snapshots"]
                      if item.effective_from <= at < item.effective_to),
    )


def test_synthetic_fixture_has_one_source_computed_accepted_plan(tmp_path: Path) -> None:
    context, seeds = _prepare(tmp_path)
    source = _source(context, seeds, TRIGGER_AT)
    assert source.status == "ACCEPTED_PLAN"
    assert source.data_request is not None and source.data_request.expected_candle_count == 526
    assert source.history_seed_verified is source.research_authorized is False


def test_complete_synthetic_source_pipeline_without_mocked_validators(tmp_path: Path) -> None:
    context, seeds = _prepare(tmp_path)
    hashes = []
    outcomes: Counter[str] = Counter()
    for day in context["scan_plan"].days:
        sources = []
        for index in range(day.time_count):
            at = day.first_confirmation + timedelta(minutes=15 * index)
            for symbol in day.symbols:
                assert symbol == "BTCUSDT"
                sources.append(_source(context, seeds, at))
            if day.symbols and (index + 1) % 12 == 0:
                print(json.dumps({"synthetic_slots_computed": index + 1,
                                  "expected_slots": day.record_count}), flush=True)
        evidence = build_source_bound_scan_day(
            **context, selection_date=day.selection_date, sources=tuple(sources),
        )
        outcomes.update(record.outcome for record in evidence.records)
        save_source_bound_scan_day(evidence, **context, sources=tuple(sources))
        hashes.append(evidence.content_hash)
        print(json.dumps({"synthetic_day_saved": str(day.selection_date),
                          "record_count": len(evidence.records)}), flush=True)
    assert outcomes == {"NO_SIGNAL": 94, "ACCEPTED_PLAN": 1}
    print("Recomputing all saved synthetic days into a complete request set", flush=True)
    request_set = build_source_bound_replay_request_set(**context, day_hashes=tuple(hashes))
    assert request_set.scan_record_count == 95 and len(request_set.requests) == 1
    request = request_set.requests[0]
    assert request.start == TRIGGER_AT
    minute = _response(tmp_path, request, _klines(request), name="synthetic-minutes.json")
    funding = _response(tmp_path, request, _funding(request), name="synthetic-funding.json",
                        endpoint="/fapi/v1/fundingRate")
    minute_artifact, _ = build_replay_market_data_artifact(
        project_dir=tmp_path, request=request, role="CANDLE_ONE_MINUTE", responses=(minute,),
    )
    funding_artifact, _ = build_replay_market_data_artifact(
        project_dir=tmp_path, request=request, role="FUNDING", responses=(funding,),
    )
    pair = DevRequestMarketDataPair(request_hash=request.request_hash,
                                    one_minute=minute_artifact, funding=funding_artifact)
    print("Revalidating synthetic scan sources and every saved market response", flush=True)
    report = verify_source_bound_request_set_market_data(
        **context, request_set=request_set, pairs=(pair,),
    )
    assert report.verified_request_count == 1 and report.one_minute_record_count == 526
    assert report.funding_record_count == 1
    assert report.strategy_evidence_verified is report.research_authorized is False
    assert report.replay_executed is False
    print(json.dumps({"synthetic_end_to_end_passed": True, "outcomes": dict(outcomes),
                      "source_receipt_bytes": sum(path.stat().st_size for path in tmp_path.glob(
                          "data/manifests/scan_trade_plan_source/*.json"
                      ))}), flush=True)
    raw = archive_path(tmp_path / "data/raw", seeds["15m"][1][0].spec)
    raw.write_bytes(raw.read_bytes() + b"synthetic corruption after successful report")
    with pytest.raises(CandleInputError, match="raw archive no longer matches"):
        require_source_bound_market_data_report(report, **context, request_set=request_set)
