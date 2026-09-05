"""Cross-day wiring mocks daily computation/storage explicitly; no genuine P3-P6 is claimed.

The final test uses real daily validation and immutable storage for an empty synthetic day.
Cross-day drift inputs are source-valid synthetic prefixes, not complete strategy evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.candle_history import ScanCandleHistory
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.request_set import DevScanDayEvidence, build_dev_replay_request_set
from slagalpha.research.scan_plan import build_dev_scan_plan
from slagalpha.research.scan_storage import restore_source_bound_scan_day
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence
from slagalpha.research.sensitivity import build_default_sensitivity_plan
from slagalpha.research.source_request_set import compute_source_bound_request_set_with_checkpoints
from slagalpha.research.splits import audit_research_inputs
from test_request_set import _replace_record, _request_set_context
from test_scan_plan import _scan_context
from test_source_request_set import _lineage_histories


@pytest.fixture
def execution_context(tmp_path: Path) -> dict[str, Any]:
    context = _request_set_context()
    return {**context, "project_dir": tmp_path, "history_days": tuple(
        (day.selection_date, ()) for day in context["scan_plan"].days
    )}


def _arguments(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key != "evidence"}


@pytest.fixture
def calls(
    execution_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    def compute(**kwargs: Any) -> tuple[DevScanDayEvidence, tuple[ScanTradePlanEvidence, ...]]:
        events.append(("compute", kwargs))
        evidence: DevScanDayEvidence = next(
            day for day in execution_context["evidence"]
            if day.day_plan.selection_date == kwargs["selection_date"]
        )
        return evidence, ()

    def save(evidence: DevScanDayEvidence, **kwargs: Any) -> Path:
        events.append(("save", {"evidence": evidence, **kwargs}))
        root: Path = execution_context["project_dir"]
        return root / "explicit-mocked-checkpoint"

    monkeypatch.setattr("slagalpha.research.source_request_set.compute_source_bound_scan_day",
                        compute)
    monkeypatch.setattr("slagalpha.research.source_request_set.save_source_bound_scan_day", save)
    return events


def test_each_day_is_saved_before_the_next_input_is_consumed_and_retries_use_fresh_lineage(
    execution_context: dict[str, Any], calls: list[tuple[str, dict[str, Any]]],
) -> None:
    context = execution_context

    def lazy_days() -> Iterator[tuple[date, tuple[tuple[ScanCandleHistory, ...], ...]]]:
        for index, item in enumerate(context["history_days"]):
            assert [kind for kind, _ in calls] == ["compute", "save"] * index
            yield item

    result = compute_source_bound_request_set_with_checkpoints(**{
        **_arguments(context), "history_days": lazy_days(),
    })
    expected = build_dev_replay_request_set(**{key: value for key, value in context.items()
                                              if key not in ("project_dir", "history_days")})
    assert result == expected
    assert result.scan_record_count == 191 and len(result.requests) == 2
    assert result.strategy_evidence_verified is result.download_authorized is False
    assert result.research_authorized is False
    assert [kind for kind, _ in calls] == ["compute", "save"] * 2
    assert calls[0][1]["history_lineage"] is calls[2][1]["history_lineage"]
    assert tuple(kwargs["evidence"].content_hash for kind, kwargs in calls if kind == "save") == (
        result.day_evidence_hashes
    )
    assert compute_source_bound_request_set_with_checkpoints(**_arguments(context)) == result
    assert calls[4][1]["history_lineage"] is calls[6][1]["history_lineage"]
    assert calls[0][1]["history_lineage"] is not calls[4][1]["history_lineage"]
    assert not (context["project_dir"] / "data").exists()  # Storage is explicitly mocked here.


@pytest.mark.parametrize(("change", "completed"), [
    ("missing", 1), ("empty", 0), ("duplicate", 1), ("reversed", 0), ("extra", 2),
    ("none_first", 0), ("none_extra", 2), ("malformed", 0), ("string_date", 0),
    ("validation_date", 0),
])
def test_missing_extra_or_misordered_days_never_return_a_partial_request_set(
    execution_context: dict[str, Any], calls: list[tuple[str, dict[str, Any]]],
    change: str, completed: int,
) -> None:
    first, second = execution_context["history_days"]
    inputs = {
        "missing": (first,), "empty": (), "duplicate": (first, first),
        "reversed": (second, first), "extra": (first, second, second),
        "none_first": (None, second), "none_extra": (first, second, None),
        "malformed": ((first[0],), second), "string_date": ((str(first[0]), ()), second),
        "validation_date": ((first[0] + timedelta(days=2), ()), second),
    }[change]
    with pytest.raises(CandleInputError, match="history day"):
        compute_source_bound_request_set_with_checkpoints(**{
            **_arguments(execution_context), "history_days": inputs,
        })
    assert [kind for kind, _ in calls] == ["compute", "save"] * completed


@pytest.mark.parametrize("change", ["registry", "parameter", "snapshots", "blocked_context"])
def test_invalid_context_is_rejected_before_consuming_or_saving_any_day(
    execution_context: dict[str, Any], calls: list[tuple[str, dict[str, Any]]], change: str,
) -> None:
    context = _arguments(execution_context)

    def forbidden() -> Iterator[tuple[date, tuple[tuple[ScanCandleHistory, ...], ...]]]:
        raise AssertionError("context gate must run before the history day stream starts")
        yield  # type: ignore[unreachable]

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
        context["snapshots"] = (first.model_copy(update={"members": (), "member_count": 0}), *rest)
    else:
        context.update({key: value for key, value in _request_set_context(ready=False).items()
                        if key != "evidence"})
    context["history_days"] = forbidden()
    with pytest.raises(ValueError):
        compute_source_bound_request_set_with_checkpoints(**context)
    assert calls == []


@pytest.mark.parametrize("failed_day", [0, 1])
@pytest.mark.parametrize("stage", ["compute", "save"])
def test_compute_and_publication_failures_stop_before_the_next_day(
    execution_context: dict[str, Any], calls: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch, failed_day: int, stage: str,
) -> None:
    import slagalpha.research.source_request_set as module

    target = ("compute_source_bound_scan_day" if stage == "compute"
              else "save_source_bound_scan_day")
    original: Callable[..., Any] = module.__dict__[target]

    def fail(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if sum(kind == stage for kind, _ in calls) == failed_day + 1:
            raise OSError("explicit synthetic compute/storage failure")
        return result

    monkeypatch.setattr(module, target, fail)
    with pytest.raises(OSError, match="synthetic compute/storage failure"):
        compute_source_bound_request_set_with_checkpoints(**_arguments(execution_context))
    expected = ["compute", "save"] * failed_day + ["compute"]
    if stage == "save":
        expected.append("save")
    assert [kind for kind, _ in calls] == expected


@pytest.mark.parametrize("state", ["BLOCKED", "NO_SIGNAL"])
def test_blocked_or_empty_requests_do_not_turn_saved_days_into_complete_success(
    execution_context: dict[str, Any], calls: list[tuple[str, dict[str, Any]]], state: str,
) -> None:
    for index in range(2):
        _replace_record(execution_context, index, 0, outcome=state, trade_plan=None,
                        reason_codes=("EXPLICIT_COMPUTATION_MOCK",))
    with pytest.raises(ValueError, match="blocked scan|empty request"):
        compute_source_bound_request_set_with_checkpoints(**_arguments(execution_context))
    # A BLOCKED day remains a diagnostic checkpoint, not a complete request set.
    assert [kind for kind, _ in calls] == ["compute", "save"] * (1 if state == "BLOCKED" else 2)


@pytest.mark.parametrize("change", ["origin", "partition"])
def test_conflicting_source_valid_prefixes_prevent_second_day_publication(
    execution_context: dict[str, Any], calls: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    import slagalpha.research.source_request_set as module

    histories = _lineage_histories(execution_context, change)
    original: Callable[..., tuple[DevScanDayEvidence, tuple[ScanTradePlanEvidence, ...]]] = (
        module.__dict__["compute_source_bound_scan_day"]
    )

    def compute(**kwargs: Any) -> tuple[DevScanDayEvidence, tuple[ScanTradePlanEvidence, ...]]:
        result = original(**kwargs)
        index = sum(kind == "compute" for kind, _ in calls) - 1
        kwargs["history_lineage"].require(histories[index])
        return result

    monkeypatch.setattr(module, "compute_source_bound_scan_day", compute)
    with pytest.raises(CandleInputError, match="origin changed|partition receipt changed"):
        compute_source_bound_request_set_with_checkpoints(**_arguments(execution_context))
    assert [kind for kind, _ in calls] == ["compute", "save", "compute"]


def test_real_empty_day_checkpoint_survives_later_failure_and_is_reusable(tmp_path: Path) -> None:
    context = _request_set_context()
    context.pop("evidence")
    first, *rest = context["snapshots"]
    context["snapshots"] = (first.model_copy(update={"members": (), "member_count": 0}), *rest)
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
    context["project_dir"] = tmp_path
    history_days = tuple((day.selection_date, ()) for day in context["scan_plan"].days)
    contents = None
    for _ in range(2):
        # First day is genuinely empty; the active second day has no required prefixes.
        with pytest.raises(CandleInputError, match="missing daily scan history bundle"):
            compute_source_bound_request_set_with_checkpoints(**context, history_days=history_days)
        paths = tuple(tmp_path.glob("data/manifests/source_scan_day/*.json"))
        assert len(paths) == 1
        observed = paths[0].read_bytes()
        if contents is not None:
            assert observed == contents
        contents = observed
        restored = restore_source_bound_scan_day(**context, content_hash=paths[0].stem)
        assert restored.records == () and restored.day_plan == context["scan_plan"].days[0]
        assert not (tmp_path / "data/manifests/replay_request_set").exists()
