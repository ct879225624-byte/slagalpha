"""Source-set orchestration uses explicit day-restorer mocks, not genuine research results."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.request_set import DevReplayRequestSet, DevScanDayEvidence
from slagalpha.research.source_request_set import (
    build_source_bound_replay_request_set,
    require_source_bound_replay_request_set,
)
from test_request_set import _rehash, _replace_record, _request_set_context
from test_scan_plan import _scan_context


@pytest.fixture
def source_set_context(tmp_path: Path) -> dict[str, Any]:
    context = _request_set_context()
    return {**context, "project_dir": tmp_path,
            "day_hashes": tuple(day.content_hash for day in context["evidence"])}


@pytest.fixture
def day_loads(
    source_set_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def mock_restore(**kwargs: Any) -> DevScanDayEvidence:
        calls.append(kwargs)
        result: DevScanDayEvidence = next(day for day in source_set_context["evidence"]
                                         if day.content_hash == kwargs["content_hash"])
        return result

    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        mock_restore)
    return calls


def _arguments(context: dict[str, Any], *, require: bool = False) -> dict[str, Any]:
    excluded = ("evidence", "day_hashes") if require else ("evidence",)
    return {key: value for key, value in context.items() if key not in excluded}


def test_every_day_is_restored_before_all_requests_are_reconciled(
    source_set_context: dict[str, Any], day_loads: list[dict[str, Any]],
) -> None:
    result = build_source_bound_replay_request_set(**_arguments(source_set_context))
    assert result.scan_record_count == 191
    assert len(result.requests) == 2
    assert tuple(call["content_hash"] for call in day_loads) == source_set_context["day_hashes"]
    assert result.strategy_evidence_verified is result.research_authorized is False
    assert result.download_authorized is False
    require_source_bound_replay_request_set(result, **_arguments(source_set_context, require=True))
    assert len(day_loads) == 4


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "invalid", "empty"])
def test_invalid_day_references_are_rejected_before_recovery(
    source_set_context: dict[str, Any], day_loads: list[dict[str, Any]], change: str,
) -> None:
    first, second = source_set_context["day_hashes"]
    altered = {"missing": (first,), "duplicate": (first, first),
               "extra": (first, second, first), "invalid": (first, "../outside"),
               "empty": ()}[change]
    with pytest.raises(ValueError):
        build_source_bound_replay_request_set(**{**_arguments(source_set_context),
                                                 "day_hashes": altered})
    assert day_loads == []


def test_reversed_days_cannot_be_reordered_silently(
    source_set_context: dict[str, Any], day_loads: list[dict[str, Any]],
) -> None:
    with pytest.raises(CandleInputError, match="exact ordered DEV plan"):
        build_source_bound_replay_request_set(**{**_arguments(source_set_context),
            "day_hashes": tuple(reversed(source_set_context["day_hashes"]))})
    assert len(day_loads) == 1


@pytest.mark.parametrize("change", ["registry", "parameter", "snapshots"])
def test_research_context_is_rechecked_before_opening_saved_days(
    source_set_context: dict[str, Any], day_loads: list[dict[str, Any]], change: str,
) -> None:
    context = _arguments(source_set_context)
    if change == "registry":
        registry = context["registry"]
        context["registry"] = ContractRegistry(registry_version=registry.registry_version, entries=(
            registry.entries[0].model_copy(update={
                "verification_status": RegistryVerification.UNVERIFIED,
            }),
        ))
    elif change == "parameter":
        context["parameter"] = _scan_context(ready=False)["parameter"]
    else:
        first, *rest = context["snapshots"]
        context["snapshots"] = (first.model_copy(update={"members": (), "member_count": 0}), *rest)
    with pytest.raises(ValueError, match="audit|belong|current source"):
        build_source_bound_replay_request_set(**context)
    assert day_loads == []


@pytest.mark.parametrize("failed_day", [0, 1])
def test_source_failure_in_any_day_prevents_request_set_creation(
    source_set_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failed_day: int,
) -> None:
    calls: list[dict[str, Any]] = []

    def failure(**kwargs: Any) -> DevScanDayEvidence:
        index = len(calls)
        calls.append(kwargs)
        if index == failed_day:
            raise CandleInputError("simulated changed original scan source")
        result: DevScanDayEvidence = source_set_context["evidence"][index]
        return result

    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        failure)
    with pytest.raises(CandleInputError, match="changed original"):
        build_source_bound_replay_request_set(**_arguments(source_set_context))
    assert len(calls) == failed_day + 1


def test_hand_selected_request_subset_still_fails_source_rebinding(
    source_set_context: dict[str, Any], day_loads: list[dict[str, Any]],
) -> None:
    result = build_source_bound_replay_request_set(**_arguments(source_set_context))
    payload = result.model_dump(mode="json")
    payload["requests"] = payload["requests"][:1]
    subset = DevReplayRequestSet.model_validate(_rehash(payload))
    with pytest.raises(CandleInputError, match="complete recomputed sources"):
        require_source_bound_replay_request_set(subset,
                                               **_arguments(source_set_context, require=True))


@pytest.mark.parametrize("state", ["BLOCKED", "NO_SIGNAL"])
def test_blocked_or_all_no_signal_days_do_not_become_empty_success(
    source_set_context: dict[str, Any], day_loads: list[dict[str, Any]], state: str,
) -> None:
    for index in range(2):
        _replace_record(source_set_context, index, 0, outcome=state, trade_plan=None,
                        reason_codes=("EXPLICIT_SOURCE_SET_MOCK",))
    source_set_context["day_hashes"] = tuple(day.content_hash
                                             for day in source_set_context["evidence"])
    with pytest.raises(ValueError, match="blocked scan|empty request"):
        build_source_bound_replay_request_set(**_arguments(source_set_context))


def test_missing_completed_checkpoint_is_not_replaced_with_declared_evidence(
    source_set_context: dict[str, Any],
) -> None:
    with pytest.raises(FileNotFoundError):
        build_source_bound_replay_request_set(**_arguments(source_set_context))


def test_recovered_receipt_must_match_the_selected_hash(
    source_set_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    substituted = source_set_context["evidence"][0].model_copy(update={"content_hash": "0" * 64})
    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        lambda **kwargs: substituted)
    with pytest.raises(CandleInputError, match="exact ordered DEV plan"):
        build_source_bound_replay_request_set(**_arguments(source_set_context))
