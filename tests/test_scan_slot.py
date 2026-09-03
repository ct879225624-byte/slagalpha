"""Synthetic raw-source slot orchestration; VERIFIED fixture rules are not real approval."""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.data.archive import archive_path
from slagalpha.data.klines import INTERVAL_MILLISECONDS
from slagalpha.domain.universe import ContractRegistry, ContractRegistryEntry, RegistryVerification
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_history import ScanCandleHistory, last_closed_boundary
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.scan_plan import build_dev_scan_plan
from slagalpha.research.scan_slot import compute_source_bound_scan_slot
from slagalpha.research.scan_trade_plan import ScanTradePlanEvidence, compute_scan_trade_plan
from slagalpha.research.sensitivity import build_default_sensitivity_plan
from slagalpha.research.splits import audit_research_inputs
from test_scan_plan import _scan_context
from test_scan_trade_plan import _trade_context


def _slot_context(
    root: Path, *, case: str = "trigger", at: datetime = datetime(2024, 1, 2, 12, 30, tzinfo=UTC),
) -> tuple[dict[str, Any], ScanTradePlanEvidence]:
    trade = _trade_context(root, case=case, at=at)
    expected = compute_scan_trade_plan(**trade)
    scan = _scan_context()
    snapshots = tuple(item.model_copy(update={
        "members": (item.members[0].model_copy(update={"symbol": "BTCUSDT"}),),
    }) for item in scan["snapshots"])
    context = {key: value for key, value in trade.items()
               if key not in ("universe", "trigger_evidence")}
    context.update(snapshots=snapshots, parameter=scan["parameter"], histories=tuple(
        item.history for item in expected.trigger_evidence.setup_evidence.features
    ))
    return context, expected


@pytest.fixture(scope="module")
def sample_slot(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _slot_context(tmp_path_factory.mktemp("synthetic-scan-slot"))[0]


def _history_update(history: ScanCandleHistory, **changes: Any) -> ScanCandleHistory:
    payload = history.model_dump(mode="json", exclude={"content_hash"})
    payload.update(changes)
    for key in ("history_start", "confirmation_close", "last_close_exclusive"):
        payload[key] = payload[key].replace("+00:00", "Z")
    return ScanCandleHistory.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def _rebind(context: dict[str, Any]) -> None:
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
    context["histories"] = tuple(_history_update(
        item, scan_plan_hash=context["scan_plan"].plan_hash,
    ) for item in context["histories"])


@pytest.mark.parametrize("case", [
    "trigger", "neutral", "eligible", "not_ready", "trigger_not_ready", "obstacle",
])
def test_entry_matches_original_stage_by_stage_computation(tmp_path: Path, case: str) -> None:
    context, expected = _slot_context(tmp_path, case=case)
    result = compute_source_bound_scan_slot(**context)
    assert result == expected
    assert result.history_seed_verified is result.research_authorized is False
    if case == "trigger":
        assert result.data_request is not None and result.data_request.expected_candle_count == 526
    if case in ("not_ready", "trigger_not_ready"):
        assert result.status == "NOT_READY" and result.data_request is None


@pytest.mark.parametrize("change", [
    "missing", "extra", "duplicate", "reversed", "mixed_time", "mixed_universe",
    "wrong_scan_plan", "wrong_universe", "invalid_last_model", "parameter",
    "snapshots", "unverified", "blocked_plan", "intraday_gap", "intraday_unverified",
    "non_dev",
])
def test_invalid_bundle_and_context_fail_before_computation(
    sample_slot: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    context = dict(sample_slot)
    histories = context["histories"]

    def must_not_compute(**kwargs: Any) -> None:
        raise AssertionError("invalid metadata must fail before P3 or source IO")

    monkeypatch.setattr("slagalpha.research.scan_slot.compute_scan_features", must_not_compute)
    if change in ("missing", "extra", "duplicate", "reversed"):
        context["histories"] = {
            "missing": histories[:-1], "extra": (*histories, histories[-1]),
            "duplicate": (*histories[:-1], histories[0]), "reversed": tuple(reversed(histories)),
        }[change]
    elif change == "mixed_time":
        context["histories"] = (*histories[:-1], _history_update(
            histories[-1], confirmation_close=(histories[-1].confirmation_close
                                               + timedelta(minutes=15)).isoformat(),
        ))
    elif change == "mixed_universe":
        context["histories"] = (*histories[:-1], _history_update(
            histories[-1], universe_content_hash="0" * 64,
        ))
    elif change in ("wrong_scan_plan", "wrong_universe"):
        key = "scan_plan_hash" if change == "wrong_scan_plan" else "universe_content_hash"
        context["histories"] = tuple(_history_update(item, **{key: "0" * 64}) for item in histories)
    elif change == "invalid_last_model":
        context["histories"] = (*histories[:-1], histories[-1].model_copy(update={"row_count": 1}))
    elif change == "parameter":
        context["parameter"] = build_dev_parameter_version(
            plan=context["plan"], candidate_hash=context["plan"].candidates[1].candidate_hash,
        )
    elif change == "snapshots":
        first, *rest = context["snapshots"]
        context["snapshots"] = (first.model_copy(update={"member_count": 0, "members": ()}), *rest)
    elif change in ("unverified", "blocked_plan", "intraday_gap", "intraday_unverified"):
        registry = context["registry"]
        rule = registry.entries[0]
        at = histories[0].confirmation_close
        entries: tuple[ContractRegistryEntry, ...] = (rule.model_copy(update={
            "verification_status": RegistryVerification.UNVERIFIED,
        }),) if change in ("unverified", "blocked_plan") else (
            rule.model_copy(update={"effective_to": at}),
        )
        if change == "intraday_unverified":
            entries += (rule.model_copy(update={
                "effective_from": at, "verification_status": RegistryVerification.UNVERIFIED,
            }),)
        context["registry"] = ContractRegistry(registry_version=registry.registry_version,
                                               entries=entries)
        if change != "unverified":
            _rebind(context)
        if change.startswith("intraday"):
            assert not context["plan"].blockers  # Daily prerequisites pass, exact slot must fail.
    else:
        at = datetime(2024, 1, 3, 12, 30, tzinfo=UTC)
        context["histories"] = tuple(_history_update(
            item, confirmation_close=at.isoformat(),
            last_close_exclusive=last_closed_boundary(at, item.interval).isoformat(),
            row_count=int((last_closed_boundary(at, item.interval) - item.history_start)
                          / timedelta(milliseconds=INTERVAL_MILLISECONDS[item.interval])),
        ) for item in histories)
    pattern = "VERIFIED active historical rule" if change.startswith("intraday") else None
    with pytest.raises(ValueError, match=pattern):
        compute_source_bound_scan_slot(**context)


def test_midnight_uses_previous_day_universe(tmp_path: Path) -> None:
    context, expected = _slot_context(tmp_path, case="neutral", at=datetime(2024, 1, 2, tzinfo=UTC))
    context["snapshots"] = tuple(reversed(context["snapshots"]))
    result = compute_source_bound_scan_slot(**context)
    assert result == expected
    assert result.universe_content_hash == context["scan_plan"].days[0].universe_content_hash
    assert result.universe_content_hash != context["scan_plan"].days[1].universe_content_hash


def test_raw_source_change_propagates_not_no_signal(
    sample_slot: dict[str, Any], tmp_path: Path,
) -> None:
    shutil.copytree(sample_slot["project_dir"], tmp_path / "copy")
    source = sample_slot["histories"][-1].sources[0]
    raw = archive_path(tmp_path / "copy/data/raw", source.spec)
    raw.write_bytes(raw.read_bytes() + b"explicit synthetic tampering")
    with pytest.raises(CandleInputError, match="raw archive no longer matches"):
        compute_source_bound_scan_slot(**{**sample_slot, "project_dir": tmp_path / "copy"})
