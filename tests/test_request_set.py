"""Synthetic scan declarations exercise coverage, not genuine P4-P6 strategy provenance."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.replay_inputs import ReplayDataInputError
from slagalpha.research.request_set import (
    DevReplayRequestSet,
    DevScanDayEvidence,
    DevScanRecord,
    build_dev_replay_request_set,
    build_dev_scan_day_evidence,
    require_replay_request_set_binding,
    write_dev_request_evidence,
)
from slagalpha.research.scan_plan import build_dev_scan_plan
from test_replay_inputs import _context
from test_research_splits import _hash
from test_scan_plan import _scan_context


def _request_set_context(
    *, accepted_times: tuple[datetime, ...] | None = None, ready: bool = True,
) -> dict[str, Any]:
    context = _scan_context(ready=ready)
    scan_plan = build_dev_scan_plan(**context)
    if accepted_times is None:
        accepted_times = (datetime(2024, 1, 1, 0, 15, tzinfo=UTC),
                          datetime(2024, 1, 2, 0, 15, tzinfo=UTC))
    plans = {}
    for at in accepted_times:
        trade = _context(at=at)["trade_plan"]
        seed = trade.entry_stop.request.model_copy(update={"logical_signal_id": _hash(str(at))})
        plans[at] = trade.model_copy(update={
            "entry_stop": trade.entry_stop.model_copy(update={"request": seed}),
        })
    evidence = []
    for day in scan_plan.days:
        records = []
        for index in range(day.time_count):
            at = day.first_confirmation + timedelta(minutes=15 * index)
            for symbol in day.symbols:
                trade = plans.get(at)
                records.append(DevScanRecord(
                    symbol=symbol, confirmation_close=at,
                    outcome="ACCEPTED_PLAN" if trade is not None else "NO_SIGNAL",
                    reason_codes=("SYNTHETIC_ACCEPTED" if trade is not None
                                  else "SYNTHETIC_NO_SIGNAL",),
                    upstream_evidence_hash=_hash(f"synthetic-source:{at}:{symbol}"),
                    trade_plan=trade,
                ))
        evidence.append(build_dev_scan_day_evidence(
            scan_plan=scan_plan, selection_date=day.selection_date, records=tuple(records),
        ))
    return {**context, "scan_plan": scan_plan, "evidence": tuple(evidence),
            "registry": _context()["registry"]}


def _replace_record(context: dict[str, Any], day_index: int, index: int, **updates: Any) -> None:
    evidence = list(context["evidence"])
    day = evidence[day_index]
    records = list(day.records)
    records[index] = records[index].model_copy(update=updates)
    evidence[day_index] = build_dev_scan_day_evidence(
        scan_plan=context["scan_plan"], selection_date=day.day_plan.selection_date,
        records=tuple(records),
    )
    context["evidence"] = tuple(evidence)


def _rehash(payload: dict[str, Any]) -> dict[str, Any]:
    payload.pop("content_hash", None)
    return {**payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}


def test_all_accepted_requests_bound_to_every_daily_record_and_persisted(tmp_path: Path) -> None:
    context = _request_set_context()
    result = build_dev_replay_request_set(**context)
    assert result.scan_record_count == 191
    assert len(result.requests) == 2
    assert result.day_evidence_hashes == tuple(item.content_hash for item in context["evidence"])
    assert result.strategy_evidence_verified is result.research_authorized is False
    assert result.download_authorized is False
    require_replay_request_set_binding(result, **context)
    context["evidence"] = iter(context["evidence"])
    assert build_dev_replay_request_set(**context) == result
    for artifact in (result, _request_set_context()["evidence"][0]):
        path = write_dev_request_evidence(artifact, tmp_path)
        assert write_dev_request_evidence(artifact, tmp_path) == path
        assert type(artifact).model_validate_json(path.read_bytes()) == artifact
        path.write_bytes(b"changed")
        with pytest.raises(ReplayDataInputError, match="changed"):
            write_dev_request_evidence(artifact, tmp_path)


def test_midnight_signal_binds_previous_universe_not_new_selection_day() -> None:
    context = _request_set_context(accepted_times=(datetime(2024, 1, 2, tzinfo=UTC),))
    result = build_dev_replay_request_set(**context)
    assert result.requests[0].universe_content_hash == (
        context["scan_plan"].days[0].universe_content_hash
    )


@pytest.mark.parametrize("mode", ["missing", "duplicate", "extra", "reversed"])
def test_daily_evidence_must_match_whole_scan_plan(mode: str) -> None:
    context = _request_set_context()
    first, second = context["evidence"]
    context["evidence"] = {
        "missing": (first,), "duplicate": (first, first),
        "extra": (first, second, second), "reversed": (second, first),
    }[mode]
    with pytest.raises(ReplayDataInputError, match="scan day"):
        build_dev_replay_request_set(**context)


@pytest.mark.parametrize("mode", ["missing", "duplicate_same_count", "extra", "reversed"])
def test_record_count_alone_cannot_prove_slot_coverage(mode: str) -> None:
    context = _request_set_context()
    day = context["evidence"][0]
    records = day.records
    changed = {
        "missing": records[:-1], "duplicate_same_count": (*records[:-1], records[0]),
        "extra": (*records, records[-1]), "reversed": tuple(reversed(records)),
    }[mode]
    with pytest.raises(ValidationError, match="exactly cover"):
        build_dev_scan_day_evidence(
            scan_plan=context["scan_plan"], selection_date=day.day_plan.selection_date,
            records=changed,
        )


def test_source_change_for_no_signal_record_invalidates_saved_set() -> None:
    context = _request_set_context()
    result = build_dev_replay_request_set(**context)
    _replace_record(context, 0, 1, upstream_evidence_hash=_hash("different-source"))
    changed = build_dev_replay_request_set(**context)
    assert changed.requests == result.requests
    assert changed.content_hash != result.content_hash
    with pytest.raises(ReplayDataInputError, match="complete scan evidence"):
        require_replay_request_set_binding(result, **context)


def test_hand_selected_valid_request_subset_fails_full_source_rebinding() -> None:
    context = _request_set_context()
    payload = build_dev_replay_request_set(**context).model_dump(mode="json")
    payload["requests"] = payload["requests"][:1]
    subset = DevReplayRequestSet.model_validate(_rehash(payload))
    with pytest.raises(ReplayDataInputError, match="complete scan evidence"):
        require_replay_request_set_binding(subset, **context)


def test_blocked_and_empty_scan_results_do_not_pass_as_complete_requests() -> None:
    context = _request_set_context()
    _replace_record(context, 0, 1, outcome="BLOCKED", reason_codes=("SYNTHETIC_MISSING_DATA",))
    with pytest.raises(ReplayDataInputError, match="blocked scan"):
        build_dev_replay_request_set(**context)
    with pytest.raises(ReplayDataInputError, match="empty request"):
        build_dev_replay_request_set(**_request_set_context(accepted_times=()))


def test_upstream_blocked_plan_fails_before_consuming_daily_evidence() -> None:
    context = _request_set_context(ready=False)

    def should_not_consume() -> Iterator[DevScanDayEvidence]:
        raise AssertionError("consumed scan evidence before input audit")
        yield  # type: ignore[unreachable]

    context["evidence"] = should_not_consume()
    with pytest.raises(ValueError, match="audit|blocked"):
        build_dev_replay_request_set(**context)


def test_current_registry_audit_is_rechecked_not_only_its_version() -> None:
    context = _request_set_context()
    registry = context["registry"]
    context["registry"] = ContractRegistry(
        registry_version=registry.registry_version,
        entries=(registry.entries[0].model_copy(update={
            "verification_status": RegistryVerification.UNVERIFIED,
        }),),
    )
    with pytest.raises(ValueError, match="audit|blocked"):
        build_dev_replay_request_set(**context)


@pytest.mark.parametrize("change", ["parameter", "members"])
def test_scan_plan_is_rebuilt_from_current_parameter_and_full_snapshots(change: str) -> None:
    context = _request_set_context()
    if change == "parameter":
        context["parameter"] = build_dev_parameter_version(
            plan=context["plan"], candidate_hash=context["plan"].candidates[1].candidate_hash,
        )
    else:
        first, *rest = context["snapshots"]
        context["snapshots"] = (first.model_copy(update={
            "members": (first.members[0].model_copy(update={"symbol": "BBBUSDT"}),),
        }), *rest)
    with pytest.raises(ReplayDataInputError, match="current source"):
        build_dev_replay_request_set(**context)


def test_duplicate_signal_ids_fail_even_across_distinct_slots() -> None:
    context = _request_set_context()
    first = context["evidence"][0].records[0].trade_plan
    second = context["evidence"][1].records[0].trade_plan
    seed = second.entry_stop.request.model_copy(update={
        "logical_signal_id": first.entry_stop.request.logical_signal_id,
    })
    second = second.model_copy(update={
        "entry_stop": second.entry_stop.model_copy(update={"request": seed}),
    })
    _replace_record(context, 1, 0, trade_plan=second)
    with pytest.raises(ValidationError, match="signal ids"):
        build_dev_replay_request_set(**context)


def test_accepted_plan_crossing_dev_is_not_silently_dropped() -> None:
    context = _request_set_context(accepted_times=(datetime(2024, 1, 2, 23, 45, tzinfo=UTC),))
    with pytest.raises(ReplayDataInputError, match="inside DEV"):
        build_dev_replay_request_set(**context)


@pytest.mark.parametrize("updates", [
    {"outcome": "NO_SIGNAL"}, {"trade_plan": None}, {"symbol": "BBBUSDT"},
    {"reason_codes": [" "]}, {"reason_codes": ["Z", "A"]},
    {"confirmation_close": "2024-01-01T00:30:00Z"},
    {"confirmation_close": "2024-01-01T00:16:00Z"},
])
def test_invalid_result_declarations_rejected(updates: dict[str, Any]) -> None:
    record = _request_set_context()["evidence"][0].records[0].model_dump(mode="json")
    with pytest.raises(ValidationError):
        DevScanRecord.model_validate({**record, **updates})


@pytest.mark.parametrize("updates", [
    {"strategy_evidence_verified": True}, {"research_authorized": True},
    {"download_authorized": True}, {"content_hash": "0" * 64},
])
def test_request_set_cannot_claim_strategy_verification_or_authorization(
    updates: dict[str, Any],
) -> None:
    result = build_dev_replay_request_set(**_request_set_context()).model_dump(mode="json")
    with pytest.raises(ValidationError):
        DevReplayRequestSet.model_validate({**result, **updates})
