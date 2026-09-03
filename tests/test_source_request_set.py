"""Source-set orchestration uses explicit day-restorer mocks, not genuine research results."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.candle_history import ScanCandleHistory, load_scan_candle_history
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.request_set import (
    DevReplayRequestSet,
    DevScanDayEvidence,
    build_dev_replay_request_set,
)
from slagalpha.research.source_request_set import (
    build_source_bound_replay_request_set,
    require_source_bound_replay_request_set,
)
from test_candle_inputs import _partition
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
    assert day_loads[0]["history_lineage"] is day_loads[1]["history_lineage"]
    assert day_loads[2]["history_lineage"] is day_loads[3]["history_lineage"]
    assert day_loads[0]["history_lineage"] is not day_loads[2]["history_lineage"]


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


def _lineage_histories(
    context: dict[str, Any], change: str,
) -> tuple[ScanCandleHistory, ScanCandleHistory]:
    """Two source-valid synthetic prefixes at actual day cutovers, not P3-P6 daily evidence."""
    first_day, second_day = context["scan_plan"].days
    root = context["project_dir"] / "v1"
    start = first_day.first_confirmation - timedelta(minutes=15)
    source = _partition(root, start=start, symbol=first_day.symbols[0], rows=97)
    arguments: dict[str, Any] = dict(
        project_dir=root, scan_plan=context["scan_plan"],
        symbol=first_day.symbols[0], interval="15m", history_start=start, sources=(source,),
    )
    first = load_scan_candle_history(
        **arguments, confirmation_close=first_day.first_confirmation,
    )[1]
    if change == "origin":
        arguments["history_start"] += timedelta(minutes=15)
    else:
        root = context["project_dir"] / "v2"
        arguments.update(project_dir=root, sources=(
            _partition(root, start=start, symbol=first_day.symbols[0], rows=97,
                       close_prices=("100",) * 97),
        ))
    second = load_scan_candle_history(
        **arguments, confirmation_close=second_day.first_confirmation,
    )[1]
    return first, second


@pytest.mark.parametrize("change", ["origin", "partition"])
@pytest.mark.parametrize("reuse", [False, True])
def test_cross_day_drift_blocks_new_and_saved_request_sets(
    source_set_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str, reuse: bool,
) -> None:
    context = source_set_context
    histories = _lineage_histories(context, change)
    calls: list[dict[str, Any]] = []

    def mock_restore(**kwargs: Any) -> DevScanDayEvidence:
        index = len(calls)
        calls.append(kwargs)
        kwargs["history_lineage"].require(histories[index])
        day: DevScanDayEvidence = context["evidence"][index]
        return day

    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        mock_restore)
    declared = build_dev_replay_request_set(**{key: value for key, value in context.items()
                                               if key not in ("project_dir", "day_hashes")})
    with pytest.raises(CandleInputError, match="origin changed|partition receipt changed"):
        if reuse:
            require_source_bound_replay_request_set(declared, **_arguments(context, require=True))
        else:
            build_source_bound_replay_request_set(**_arguments(context))
    assert len(calls) == 2 and calls[0]["history_lineage"] is calls[1]["history_lineage"]
