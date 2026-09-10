"""Synthetic metadata fixtures only; these are not verified market rule evidence."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from slagalpha.domain.universe import (
    ContractRegistry,
    EvidenceConfidence,
    RegistryVerification,
    UniverseMember,
    UniverseSnapshot,
)
from slagalpha.research.rule_gaps import (
    DevRuleGapReport,
    build_dev_rule_gap_report,
    write_dev_rule_gap_report,
)
from slagalpha.research.splits import (
    ResearchSplitError,
    ResearchSplitManifest,
    build_research_split,
    snapshot_sequence_hash,
)
from test_research_splits import _snapshot, _unverified_registry


def _blocked_registry() -> ContractRegistry:
    registry = _unverified_registry()
    return ContractRegistry(
        registry_version=registry.registry_version,
        entries=(registry.entries[0].model_copy(update={
            "confidence": EvidenceConfidence.LOW,
        }),),
    )


def _inputs() -> tuple[ResearchSplitManifest, tuple[UniverseSnapshot, ...]]:
    start = date(2024, 1, 1)
    snapshots = tuple(_snapshot(start + timedelta(days=offset)) for offset in range(8))
    split = build_research_split(
        research_start=start,
        research_end_exclusive=start + timedelta(days=8),
        universe_batch_run_version="a" * 64,
        daily_snapshot_hash=snapshot_sequence_hash(snapshots),
    )
    return split, snapshots


def test_gaps_are_dev_only_contiguous_and_deterministic(tmp_path: Path) -> None:
    split, snapshots = _inputs()
    registry = _blocked_registry()
    report = build_dev_rule_gap_report(split=split, snapshots=snapshots, registry=registry)
    assert report.blocked_member_day_count == 4
    assert report.eligible_member_day_count == 0
    assert len(report.targets) == 1
    assert len(report.targets[0].windows) == 1
    assert report.targets[0].windows[0].end_exclusive == date(2024, 1, 5)
    assert report.coverage_scope == "EFFECTIVE_FROM_GATE_ONLY"
    assert report.strategy_executed is report.locked_test_consumed is False
    assert build_dev_rule_gap_report(
        split=split, snapshots=tuple(reversed(snapshots)), registry=registry
    ) == report
    path = write_dev_rule_gap_report(report, tmp_path)
    assert DevRuleGapReport.model_validate_json(path.read_bytes()) == report
    assert write_dev_rule_gap_report(report, tmp_path) == path
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchSplitError, match="changed"):
        write_dev_rule_gap_report(report, tmp_path)


def test_reason_changes_and_verified_days_do_not_bridge_gaps() -> None:
    split, snapshots = _inputs()
    entry = _blocked_registry().entries[0]
    # Day 1 missing, day 2 unverified, day 3 verified, day 4 missing.
    unverified = entry.model_copy(update={
        "effective_from": snapshots[1].effective_from,
        "effective_to": snapshots[2].effective_from,
    })
    verified = entry.model_copy(update={
        "effective_from": snapshots[2].effective_from,
        "effective_to": snapshots[3].effective_from,
        "verification_status": RegistryVerification.VERIFIED,
    })
    registry = ContractRegistry(registry_version="fixture", entries=(unverified, verified))
    report = build_dev_rule_gap_report(split=split, snapshots=snapshots, registry=registry)
    assert report.eligible_member_day_count == 1
    assert report.blocked_member_day_count == 3
    assert [w.start for w in report.targets[0].windows] == [
        date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 4)
    ]
    assert [w.reason_codes[0].value for w in report.targets[0].windows] == [
        "CONTRACT_RULE_MISSING", "CONTRACT_RULE_UNVERIFIED", "CONTRACT_RULE_MISSING"
    ]


def test_gaps_reject_incomplete_input_or_tampered_report() -> None:
    split, snapshots = _inputs()
    registry = _blocked_registry()
    with pytest.raises(ResearchSplitError, match="exactly cover"):
        build_dev_rule_gap_report(split=split, snapshots=snapshots[:-1], registry=registry)
    report = build_dev_rule_gap_report(split=split, snapshots=snapshots, registry=registry)
    payload = report.model_dump(mode="json")
    payload["registry_content_hash"] = "b" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        DevRuleGapReport.model_validate(payload)
    payload = report.model_dump(mode="json")
    payload["dataset_role"] = "LOCKED_TEST"
    with pytest.raises(ValueError, match="DEV"):
        DevRuleGapReport.model_validate(payload)


def test_empty_queue_when_synthetic_rules_cover_all_dev_gates() -> None:
    split, snapshots = _inputs()
    entry = _unverified_registry().entries[0].model_copy(update={
        "verification_status": RegistryVerification.VERIFIED,
    })
    report = build_dev_rule_gap_report(
        split=split, snapshots=snapshots,
        registry=ContractRegistry(registry_version="fixture", entries=(entry,)),
    )
    assert report.targets == ()
    assert report.blocked_member_day_count == 0
    assert report.eligible_member_day_count == 4


def test_targets_sort_by_count_then_symbol_without_bridging_absent_membership() -> None:
    split, snapshots = _inputs()
    revised = []
    for index, snapshot in enumerate(snapshots):
        member = snapshot.members[0]
        members: tuple[UniverseMember, ...] = (
            member.model_copy(update={"symbol": "BBBUSDT"}),
            member.model_copy(update={"symbol": "CCCUSDT", "rank": 2}),
        )
        if index != 1:
            members = (*members, member.model_copy(update={"rank": 3}))
        revised.append(snapshot.model_copy(update={
            "members": members, "member_count": len(members),
        }))
    split = build_research_split(
        research_start=split.research_start,
        research_end_exclusive=split.research_end_exclusive,
        universe_batch_run_version=split.universe_batch_run_version,
        daily_snapshot_hash=snapshot_sequence_hash(tuple(revised)),
    )
    report = build_dev_rule_gap_report(
        split=split, snapshots=tuple(revised), registry=_blocked_registry()
    )
    assert [(t.symbol, t.blocked_member_day_count) for t in report.targets] == [
        ("BBBUSDT", 4), ("CCCUSDT", 4), ("AAAUSDT", 3)
    ]
    assert [(w.start, w.end_exclusive) for w in report.targets[-1].windows] == [
        (date(2024, 1, 1), date(2024, 1, 2)),
        (date(2024, 1, 3), date(2024, 1, 5)),
    ]
