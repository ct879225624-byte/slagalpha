"""All prices, archives and VERIFIED rules in this module are explicit synthetic fixtures."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from slagalpha.data.archive import archive_path
from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.scan_records import (
    build_source_bound_scan_record,
    require_source_bound_scan_record,
)
from slagalpha.research.scan_trade_plan import compute_scan_trade_plan
from test_scan_trade_plan import _trade_context


def _record_context(root: Path, case: str = "trigger") -> dict[str, Any]:
    context = _trade_context(root, case="trigger" if case == "entry_rejected" else case)
    if case == "entry_rejected":
        registry = context["registry"]
        context["registry"] = ContractRegistry(registry_version=registry.registry_version, entries=(
            registry.entries[0].model_copy(update={"tick_size": Decimal("10")}),
        ))
    evidence = compute_scan_trade_plan(**context)
    context.pop("trigger_evidence")
    return {**context, "evidence": evidence}


@pytest.mark.parametrize(("case", "outcome", "reason"), [
    ("trigger", "ACCEPTED_PLAN", "P6:ACCEPTED_PLAN"),
    ("entry_rejected", "REJECTED_PLAN", "P6:REJECTED_ENTRY_STOP"),
    ("obstacle", "REJECTED_PLAN", "P6:TAKE_PROFIT:OBSTACLE_LT_1R"),
    ("neutral", "NO_SIGNAL", "P4:FOUR_HOUR_NEUTRAL"),
    ("compressed", "NO_SIGNAL", "P4:FOUR_HOUR_COMPRESSED"),
    ("daily_block", "NO_SIGNAL", "P4:DAILY_BLOCK_LONG"),
    ("no_pullback", "NO_SIGNAL", "P4:1H:PULLBACK_NONE"),
    ("eligible", "NO_SIGNAL", "P5:NO_CONFIRMED_TRIGGER"),
    ("not_ready", "BLOCKED", "P4:NOT_READY"),
    ("trigger_not_ready", "BLOCKED", "P5:NOT_READY"),
])
def test_source_pipeline_preserves_slot_outcomes(
    tmp_path: Path, case: str, outcome: str, reason: str,
) -> None:
    context = _record_context(tmp_path, case)
    record = build_source_bound_scan_record(**context)
    evidence = context["evidence"]
    history = evidence.trigger_evidence.setup_evidence.features[0].history
    assert record.outcome == outcome
    assert reason in record.reason_codes
    assert record.reason_codes == tuple(sorted(set(record.reason_codes)))
    assert record.upstream_evidence_hash == evidence.content_hash
    assert record.confirmation_close == history.confirmation_close
    assert record.symbol == history.symbol
    assert (record.trade_plan is not None) == (outcome == "ACCEPTED_PLAN")
    assert evidence.history_seed_verified is evidence.research_authorized is False
    require_source_bound_scan_record(record, **context)


@pytest.mark.parametrize("change", ["outcome", "reason", "hash", "symbol", "time"])
def test_internally_valid_record_cannot_replace_recomputed_result(
    tmp_path: Path, change: str,
) -> None:
    context = _record_context(tmp_path, "neutral")
    record = build_source_bound_scan_record(**context)
    changes: dict[str, dict[str, Any]] = {
        "outcome": {"outcome": "REJECTED_PLAN"},
        "reason": {"reason_codes": ("ARBITRARY_REASON",)},
        "hash": {"upstream_evidence_hash": "0" * 64},
        "symbol": {"symbol": "ETHUSDT"},
        "time": {"confirmation_close": record.confirmation_close + timedelta(minutes=15)},
    }
    with pytest.raises(CandleInputError, match="recomputed source evidence"):
        require_source_bound_scan_record(record.model_copy(update=changes[change]), **context)


def test_saved_record_does_not_bypass_changed_archive(tmp_path: Path) -> None:
    context = _record_context(tmp_path, "neutral")
    record = build_source_bound_scan_record(**context)
    source = context["evidence"].trigger_evidence.setup_evidence.features[0].history.sources[0]
    raw = archive_path(tmp_path / "data" / "raw", source.spec)
    # Test-only byte mutation: the saved record itself remains unchanged.
    raw.write_bytes(raw.read_bytes() + b"changed synthetic source")
    with pytest.raises(CandleInputError, match="raw archive no longer matches"):
        require_source_bound_scan_record(record, **context)


def test_rule_regression_is_not_converted_into_no_signal(tmp_path: Path) -> None:
    context = _record_context(tmp_path, "neutral")
    registry = context["registry"]
    context["registry"] = ContractRegistry(registry_version=registry.registry_version, entries=(
        registry.entries[0].model_copy(update={
            "verification_status": RegistryVerification.UNVERIFIED,
        }),
    ))
    context["project_dir"] = tmp_path / "missing"
    with pytest.raises(CandleInputError, match="VERIFIED"):
        build_source_bound_scan_record(**context)
