"""Daily computation wiring mocks slot boundaries explicitly; selected last slots use raw files.

Most bundles below are geometry-valid declarations copied from an existing synthetic fixture,
not genuine P3-P6 evidence. Only the explicitly selected final slot uses real validators.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.candle_history import ScanCandleHistory, ScanHistoryLineage
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.request_set import DevScanRecord
from slagalpha.research.scan_days import compute_source_bound_scan_day
from slagalpha.research.scan_plan import build_dev_scan_plan
from slagalpha.research.scan_records import build_source_bound_scan_record
from slagalpha.research.scan_slot import compute_source_bound_scan_slot
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence
from slagalpha.research.sensitivity import build_default_sensitivity_plan
from slagalpha.research.splits import audit_research_inputs
from test_scan_days import sample_day as sample_day
from test_scan_days import slot_calls as slot_calls
from test_scan_history_lineage import _history_rehash
from test_scan_plan import _scan_context


@pytest.fixture
def compute_context(sample_day: dict[str, Any]) -> dict[str, Any]:
    bundles = tuple(tuple(feature.history
                          for feature in source.trigger_evidence.setup_evidence.features)
                    for source in sample_day["sources"])
    return {**{key: value for key, value in sample_day.items() if key != "sources"},
            "history_bundles": bundles}


@pytest.fixture
def computations(
    sample_day: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    by_slot: dict[tuple[str, datetime], ScanTradePlanEvidence] = {}
    for source in sample_day["sources"]:
        history = source.trigger_evidence.setup_evidence.features[0].history
        by_slot[(history.symbol, history.confirmation_close)] = source

    def mocked_slot(**kwargs: Any) -> ScanTradePlanEvidence:
        calls.append(kwargs)
        first = kwargs["histories"][0]
        return by_slot[(first.symbol, first.confirmation_close)]

    monkeypatch.setattr("slagalpha.research.scan_days.compute_source_bound_scan_slot", mocked_slot)
    return calls


def test_computation_and_revalidation_consume_one_slot_at_a_time(
    compute_context: dict[str, Any], sample_day: dict[str, Any],
    computations: list[dict[str, Any]], slot_calls: list[dict[str, Any]],
) -> None:
    def lazy_bundles() -> Iterator[tuple[ScanCandleHistory, ...]]:
        for index, bundle in enumerate(compute_context["history_bundles"]):
            assert len(computations) == len(slot_calls) == index
            yield bundle

    evidence, sources = compute_source_bound_scan_day(**{**compute_context,
                                                         "history_bundles": lazy_bundles()})
    assert len(computations) == len(slot_calls) == len(evidence.records) == 96
    assert sources == sample_day["sources"]
    assert tuple(record.upstream_evidence_hash for record in evidence.records) == tuple(
        source.content_hash for source in sources
    )
    assert evidence.records[-1].confirmation_close == datetime(2024, 1, 2, tzinfo=UTC)
    assert slot_calls[-1]["universe"].selected_at.date() == compute_context["selection_date"]
    assert evidence.strategy_evidence_verified is False
    assert not (compute_context["project_dir"] / "data/manifests/source_scan_day").exists()


@pytest.mark.parametrize(("change", "count"), [
    ("missing", 95), ("duplicate", 95), ("extra", 96), ("reversed", 0),
    ("none_first", 0), ("none_extra", 96), ("short", 0), ("interval_order", 0),
    ("invalid_type", 0), ("wrong_universe", 0), ("wrong_plan", 0), ("invalid_hash", 0),
])
def test_history_bundle_errors_fail_at_the_exact_boundary(
    compute_context: dict[str, Any], computations: list[dict[str, Any]],
    slot_calls: list[dict[str, Any]], change: str, count: int,
) -> None:
    bundles = compute_context["history_bundles"]
    altered: Any
    if change in ("wrong_universe", "wrong_plan", "invalid_hash"):
        key = {"wrong_universe": "universe_content_hash", "wrong_plan": "scan_plan_hash",
               "invalid_hash": "content_hash"}[change]
        altered = ((*bundles[0][:-1], bundles[0][-1].model_copy(update={key: "0" * 64})),
                   *bundles[1:])
    else:
        altered = {
            "missing": bundles[:-1], "duplicate": (*bundles[:-1], bundles[0]),
            "extra": (*bundles, bundles[-1]), "reversed": tuple(reversed(bundles)),
            "none_first": (None, *bundles[1:]), "none_extra": (*bundles, None),
            "short": (bundles[0][:-1], *bundles[1:]),
            "interval_order": (tuple(reversed(bundles[0])), *bundles[1:]),
            "invalid_type": ((None, *bundles[0][1:]), *bundles[1:]),
        }[change]
    with pytest.raises(ValueError):
        compute_source_bound_scan_day(**{**compute_context, "history_bundles": altered})
    assert len(computations) == len(slot_calls) == count


@pytest.mark.parametrize("change", ["registry", "parameter", "snapshots", "date", "lineage"])
def test_global_context_is_checked_before_history_generator_is_started(
    compute_context: dict[str, Any], computations: list[dict[str, Any]],
    slot_calls: list[dict[str, Any]], change: str,
) -> None:
    context = dict(compute_context)

    def forbidden() -> Iterator[tuple[ScanCandleHistory, ...]]:
        raise AssertionError("invalid global metadata must not consume history bundles")
        yield  # type: ignore[unreachable]

    context["history_bundles"] = forbidden()
    if change == "registry":
        registry = context["registry"]
        context["registry"] = ContractRegistry(registry_version=registry.registry_version, entries=(
            registry.entries[0].model_copy(update={
                "verification_status": RegistryVerification.UNVERIFIED,
            }),
        ))
    elif change == "parameter":
        context["parameter"] = _scan_context(ready=False)["parameter"]
    elif change == "snapshots":
        first, *rest = context["snapshots"]
        context["snapshots"] = (first.model_copy(update={"member_count": 0, "members": ()}), *rest)
    elif change == "date":
        context["selection_date"] += timedelta(days=2)
    else:
        context["history_lineage"] = ScanHistoryLineage("0" * 64)
    with pytest.raises(ValueError):
        compute_source_bound_scan_day(**context)
    assert not computations and not slot_calls


@pytest.mark.parametrize("change", ["origin", "partition"])
def test_lineage_drift_is_rejected_before_computing_the_changed_slot(
    compute_context: dict[str, Any], computations: list[dict[str, Any]],
    slot_calls: list[dict[str, Any]], change: str,
) -> None:
    bundles = compute_context["history_bundles"]
    history = bundles[1][0]
    payload = history.model_dump(mode="json")
    if change == "origin":
        start = history.history_start + timedelta(minutes=15)
        payload["history_start"] = start.isoformat().replace("+00:00", "Z")
        payload["row_count"] -= 1
    else:
        payload["sources"][0]["normalization"]["parquet_sha256"] = "0" * 64
    changed = (_history_rehash(payload), *bundles[1][1:])
    with pytest.raises(CandleInputError, match="origin changed|partition receipt changed"):
        compute_source_bound_scan_day(**{**compute_context,
            "history_bundles": (bundles[0], changed, *bundles[2:])})
    assert len(computations) == len(slot_calls) == 1


@pytest.mark.parametrize("extra", [False, True])
def test_empty_universe_computes_nothing_and_requires_empty_bundles(
    compute_context: dict[str, Any], computations: list[dict[str, Any]],
    slot_calls: list[dict[str, Any]], extra: bool,
) -> None:
    context = dict(compute_context)
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
    context["history_bundles"] = (context["history_bundles"][0],) if extra else ()
    if extra:
        with pytest.raises(CandleInputError, match="extra daily scan history bundle"):
            compute_source_bound_scan_day(**context)
    else:
        evidence, sources = compute_source_bound_scan_day(**context)
        assert evidence.records == ()
        assert sources == ()
    assert not computations and not slot_calls


@pytest.mark.parametrize("missing_raw", [False, True])
def test_final_slot_computes_and_revalidates_actual_synthetic_files(
    compute_context: dict[str, Any], sample_day: dict[str, Any],
    computations: list[dict[str, Any]], slot_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing_raw: bool,
) -> None:
    import slagalpha.research.scan_days as module

    mocked_compute: Callable[..., ScanTradePlanEvidence] = (
        module.__dict__["compute_source_bound_scan_slot"]
    )
    mocked_record: Callable[..., DevScanRecord] = module.__dict__["build_source_bound_scan_record"]
    last = compute_context["history_bundles"][-1][0].confirmation_close

    def final_real_compute(**kwargs: Any) -> ScanTradePlanEvidence:
        if kwargs["histories"][0].confirmation_close != last:
            return mocked_compute(**kwargs)
        if missing_raw:
            kwargs["project_dir"] = tmp_path / "missing"
        return compute_source_bound_scan_slot(**kwargs)

    def final_real_record(**kwargs: Any) -> DevScanRecord:
        history = kwargs["evidence"].trigger_evidence.setup_evidence.features[0].history
        if history.confirmation_close != last:
            return mocked_record(**kwargs)
        return build_source_bound_scan_record(**kwargs)

    monkeypatch.setattr(module, "compute_source_bound_scan_slot", final_real_compute)
    monkeypatch.setattr(module, "build_source_bound_scan_record", final_real_record)
    if missing_raw:
        with pytest.raises(CandleInputError, match="missing"):
            compute_source_bound_scan_day(**compute_context)
    else:
        evidence, sources = compute_source_bound_scan_day(**compute_context)
        assert sources[-1] == sample_day["sources"][-1]
        assert "P4:FOUR_HOUR_NEUTRAL" in evidence.records[-1].reason_codes
    assert len(computations) == len(slot_calls) == 95
