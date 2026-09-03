"""Daily orchestration mocks the already-tested slot boundary; one slot uses raw synthetic files.

Copied slot stubs below deliberately are not valid P3-P6 evidence. Only explicit mocked tests
use them; the real adapter rejects them. This module never claims a full raw-data DEV scan.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.data.klines import INTERVAL_MILLISECONDS
from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_history import (
    ScanCandleHistory,
    ScanHistoryLineage,
    last_closed_boundary,
)
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.request_set import DevScanRecord, build_dev_scan_day_evidence
from slagalpha.research.scan_days import build_source_bound_scan_day, require_source_bound_scan_day
from slagalpha.research.scan_plan import build_dev_scan_plan
from slagalpha.research.scan_records import build_source_bound_scan_record
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence, compute_scan_trade_plan
from slagalpha.research.sensitivity import build_default_sensitivity_plan
from slagalpha.research.splits import audit_research_inputs
from test_research_splits import _hash
from test_scan_history_lineage import _history_rehash
from test_scan_plan import _scan_context
from test_scan_trade_plan import _trade_context


def _stub_history_at(history: ScanCandleHistory, at: datetime) -> ScanCandleHistory:
    """Valid receipt geometry only; copied content claims are NOT source-verified evidence."""
    end = last_closed_boundary(at, history.interval)
    payload = history.model_dump(mode="json", exclude={"content_hash"})
    payload.update(
        confirmation_close=at.isoformat().replace("+00:00", "Z"),
        last_close_exclusive=end.isoformat().replace("+00:00", "Z"),
        row_count=int((end - history.history_start)
                      / timedelta(milliseconds=INTERVAL_MILLISECONDS[history.interval])),
        sources=[item.model_dump(mode="json") for item in history.sources
                 if item.spec.period <= (end - timedelta(microseconds=1)).strftime("%Y-%m")],
    )
    return ScanCandleHistory.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


@pytest.fixture(scope="module")
def sample_day(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    context = _trade_context(tmp_path_factory.mktemp("scan-day-sources"), case="neutral",
                             at=datetime(2024, 1, 2, tzinfo=UTC))
    real_source = compute_scan_trade_plan(**context)
    context.pop("trigger_evidence")
    context.pop("universe")
    scan = _scan_context()
    context["snapshots"] = tuple(snapshot.model_copy(update={
        "members": (snapshot.members[0].model_copy(update={"symbol": "BTCUSDT"}),),
    }) for snapshot in scan["snapshots"])
    context["parameter"] = scan["parameter"]
    day = context["scan_plan"].days[0]
    stubs = []
    for index in range(day.time_count):
        at = day.first_confirmation + timedelta(minutes=15 * index)
        setup = real_source.trigger_evidence.setup_evidence
        stub_setup = setup.model_copy(update={"features": tuple(feature.model_copy(update={
            "history": _stub_history_at(feature.history, at),
        }) for feature in setup.features)})
        stubs.append(real_source.model_copy(update={
            "trigger_evidence": real_source.trigger_evidence.model_copy(update={
                "setup_evidence": stub_setup,
            }), "content_hash": _hash(f"explicit slot stub:{at}"),
        }))
    stubs[-1] = real_source
    return {**context, "selection_date": day.selection_date, "sources": tuple(stubs)}


@pytest.fixture
def slot_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def stub_adapter(**kwargs: Any) -> DevScanRecord:
        calls.append(kwargs)
        source = kwargs["evidence"]
        history = source.trigger_evidence.setup_evidence.features[0].history
        return DevScanRecord(
            symbol=history.symbol, confirmation_close=history.confirmation_close,
            outcome="NO_SIGNAL", reason_codes=("EXPLICIT_SLOT_BOUNDARY_MOCK",),
            upstream_evidence_hash=source.content_hash,
        )

    monkeypatch.setattr("slagalpha.research.scan_days.build_source_bound_scan_record", stub_adapter)
    return calls


def test_every_slot_is_validated_and_midnight_keeps_previous_universe(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]],
) -> None:
    day = build_source_bound_scan_day(**{**sample_day, "sources": iter(sample_day["sources"])})
    assert len(slot_calls) == len(day.records) == 96
    assert day.strategy_evidence_verified is False
    assert all(call["universe"].selected_at.date() == sample_day["selection_date"]
               for call in slot_calls)
    assert day.records[-1].confirmation_close == datetime(2024, 1, 2, tzinfo=UTC)
    context = {key: value for key, value in sample_day.items() if key != "selection_date"}
    require_source_bound_scan_day(day, **context)
    assert len(slot_calls) == 192


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "reversed"])
def test_daily_sources_must_cover_exact_slots(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]], change: str,
) -> None:
    sources = sample_day["sources"]
    altered = {"missing": sources[:-1], "duplicate": (*sources[:-1], sources[0]),
               "extra": (*sources, sources[-1]), "reversed": tuple(reversed(sources))}[change]
    with pytest.raises(CandleInputError, match="daily scan source"):
        build_source_bound_scan_day(**{**sample_day, "sources": altered})


@pytest.mark.parametrize("change", ["registry", "parameter", "universe", "date"])
def test_invalid_context_rejected_before_consuming_sources(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]], change: str,
) -> None:
    context = dict(sample_day)

    def unconsumed() -> Iterator[ScanTradePlanEvidence]:
        raise AssertionError("must reject research context before consuming sources")
        yield  # type: ignore[unreachable]

    context["sources"] = unconsumed()
    if change == "registry":
        registry = context["registry"]
        context["registry"] = ContractRegistry(registry_version=registry.registry_version, entries=(
            registry.entries[0].model_copy(update={
                "verification_status": RegistryVerification.UNVERIFIED,
            }),
        ))
    elif change == "parameter":
        context["parameter"] = _scan_context(ready=False)["parameter"]
    elif change == "universe":
        first, *rest = context["snapshots"]
        context["snapshots"] = (first.model_copy(update={"member_count": 0, "members": ()}), *rest)
    else:
        context["selection_date"] += timedelta(days=2)
    with pytest.raises(ValueError, match="audit|context|belong|DEV"):
        build_source_bound_scan_day(**context)
    assert slot_calls == []


def test_saved_no_signal_reason_is_recomputed_not_just_hash_checked(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]],
) -> None:
    day = build_source_bound_scan_day(**sample_day)
    changed = build_dev_scan_day_evidence(
        scan_plan=sample_day["scan_plan"], selection_date=sample_day["selection_date"],
        records=(*day.records[:-1], day.records[-1].model_copy(update={
            "reason_codes": ("FORGED_NO_SIGNAL",),
        })),
    )
    context = {key: value for key, value in sample_day.items() if key != "selection_date"}
    with pytest.raises(CandleInputError, match="recomputed records"):
        require_source_bound_scan_day(changed, **context)


@pytest.mark.parametrize("changed_source", [False, True])
def test_final_slot_uses_real_raw_revalidation(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
    changed_source: bool, tmp_path: Path,
) -> None:
    import slagalpha.research.scan_days as module

    stub_adapter: Callable[..., DevScanRecord] = module.__dict__["build_source_bound_scan_record"]
    real_source = sample_day["sources"][-1]

    def last_real_adapter(**kwargs: Any) -> DevScanRecord:
        if kwargs["evidence"] is real_source:
            if changed_source:
                kwargs["project_dir"] = tmp_path / "missing-raw-sources"
            return build_source_bound_scan_record(**kwargs)
        return stub_adapter(**kwargs)

    monkeypatch.setattr(module, "build_source_bound_scan_record", last_real_adapter)
    if changed_source:
        with pytest.raises(CandleInputError, match="missing"):
            build_source_bound_scan_day(**sample_day)
    else:
        day = build_source_bound_scan_day(**sample_day)
        assert day.records[-1].reason_codes != ("EXPLICIT_SLOT_BOUNDARY_MOCK",)
        assert "P4:FOUR_HOUR_NEUTRAL" in day.records[-1].reason_codes
    assert len(slot_calls) == 95


@pytest.mark.parametrize("position", ["first", "extra"])
def test_none_is_not_an_end_of_stream_marker(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]], position: str,
) -> None:
    sources = sample_day["sources"]
    altered = (None, *sources) if position == "first" else (*sources, None, sources[0])
    with pytest.raises(CandleInputError, match="daily scan source"):
        build_source_bound_scan_day(**{**sample_day, "sources": altered})


@pytest.mark.parametrize("extra_source", [False, True])
def test_empty_universe_still_requires_exact_empty_sources(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]], extra_source: bool,
) -> None:
    context = dict(sample_day)
    first, *rest = context["snapshots"]
    context["snapshots"] = (first.model_copy(update={"member_count": 0, "members": ()}), *rest)
    audit = audit_research_inputs(split=context["split"], snapshots=context["snapshots"],
                                  registry=context["registry"])
    context["plan"] = build_default_sensitivity_plan(
        split=context["split"], audit=audit,
        strategy_rules_sha256=context["plan"].strategy_rules_sha256,
    )
    context["parameter"] = build_dev_parameter_version(
        plan=context["plan"], candidate_hash=context["plan"].candidates[0].candidate_hash,
    )
    context["scan_plan"] = build_dev_scan_plan(**{
        key: context[key] for key in ("split", "plan", "parameter", "snapshots")
    })
    context["sources"] = (sample_day["sources"][0],) if extra_source else ()
    if extra_source:
        with pytest.raises(CandleInputError, match="extra daily scan source"):
            build_source_bound_scan_day(**context)
    else:
        result = build_source_bound_scan_day(**context)
        assert result.records == ()
        assert result.strategy_evidence_verified is False
    assert slot_calls == []


def test_not_ready_slot_is_preserved_as_blocked(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import slagalpha.research.scan_days as module

    adapter: Callable[..., DevScanRecord] = module.__dict__["build_source_bound_scan_record"]

    def blocked_adapter(**kwargs: Any) -> DevScanRecord:
        return adapter(**kwargs).model_copy(update={
            "outcome": "BLOCKED", "reason_codes": ("P4:NOT_READY",),
        })

    monkeypatch.setattr(module, "build_source_bound_scan_record", blocked_adapter)
    result = build_source_bound_scan_day(**sample_day)
    assert len(result.records) == 96
    assert all(record.outcome == "BLOCKED" for record in result.records)


@pytest.mark.parametrize("interval_index", range(4))
@pytest.mark.parametrize("change", ["origin", "partition"])
def test_daily_lineage_drift_is_rejected_even_for_no_signal_slots(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]],
    interval_index: int, change: str,
) -> None:
    sources = list(sample_day["sources"])
    source = sources[1]
    setup = source.trigger_evidence.setup_evidence
    features = list(setup.features)
    history = features[interval_index].history
    payload = history.model_dump(mode="json")
    if change == "origin":
        step = timedelta(milliseconds=INTERVAL_MILLISECONDS[history.interval])
        payload["history_start"] = (history.history_start + step).isoformat().replace("+00:00", "Z")
        payload["row_count"] -= 1
    else:
        payload["sources"][0]["normalization"]["parquet_sha256"] = "0" * 64
    features[interval_index] = features[interval_index].model_copy(update={
        "history": _history_rehash(payload),
    })
    sources[1] = source.model_copy(update={
        "trigger_evidence": source.trigger_evidence.model_copy(update={
            "setup_evidence": setup.model_copy(update={"features": tuple(features)}),
        }),
    })
    with pytest.raises(CandleInputError, match="origin changed|partition receipt changed"):
        build_source_bound_scan_day(**{**sample_day, "sources": tuple(sources)})
    assert len(slot_calls) == 1


@pytest.mark.parametrize("restored", [False, True])
def test_day_must_honor_origin_previously_seen_by_the_shared_lineage(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]], restored: bool,
) -> None:
    history = sample_day["sources"][0].trigger_evidence.setup_evidence.features[0].history
    payload = history.model_dump(mode="json")
    payload["history_start"] = (history.history_start + timedelta(minutes=15)).isoformat().replace(
        "+00:00", "Z",
    )
    payload["row_count"] -= 1
    lineage = ScanHistoryLineage(sample_day["scan_plan"].plan_hash)
    lineage.require(_history_rehash(payload))
    evidence = build_source_bound_scan_day(**sample_day) if restored else None
    slot_calls.clear()
    with pytest.raises(CandleInputError, match="origin changed"):
        if evidence is not None:
            context = {key: value for key, value in sample_day.items() if key != "selection_date"}
            require_source_bound_scan_day(evidence, **context, history_lineage=lineage)
        else:
            build_source_bound_scan_day(**sample_day, history_lineage=lineage)
    assert not slot_calls


def test_lineage_from_another_scan_plan_fails_before_consuming_sources(
    sample_day: dict[str, Any], slot_calls: list[dict[str, Any]],
) -> None:
    def forbidden() -> Iterator[ScanTradePlanEvidence]:
        raise AssertionError("different scan plan must fail before source consumption")
        yield  # type: ignore[unreachable]

    with pytest.raises(CandleInputError, match="different scan plan"):
        build_source_bound_scan_day(**{**sample_day, "sources": forbidden()},
                                     history_lineage=ScanHistoryLineage("0" * 64))
    assert not slot_calls
