"""Tests for frozen P9 time splits and fail-closed input audits."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from slagalpha.domain.universe import (
    ContractRegistry,
    ContractRegistryEntry,
    EvidenceConfidence,
    RegistryVerification,
    UniverseMember,
    UniverseSnapshot,
)
from slagalpha.research.splits import (
    DatasetRole,
    ResearchReadiness,
    ResearchSplitError,
    audit_research_inputs,
    build_research_split,
    require_research_role,
    role_for_date,
    snapshot_sequence_hash,
    write_research_split,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot(day: date) -> UniverseSnapshot:
    selected_at = datetime(day.year, day.month, day.day, 0, 5, tzinfo=UTC)
    member = UniverseMember(
        symbol="AAAUSDT",
        rank=1,
        rolling_quote_volume_24h=Decimal("1000"),
        forced=False,
        eligibility_refs=("fixture:identity",),
    )
    return UniverseSnapshot(
        universe_version=_hash(day.isoformat()),
        selection_algorithm_version="fixture",
        selected_at=selected_at,
        effective_from=selected_at.replace(minute=15),
        effective_to=selected_at.replace(minute=15) + timedelta(days=1),
        ranking_window_start=selected_at.replace(minute=0) - timedelta(days=1),
        ranking_window_end=selected_at.replace(minute=0),
        member_count=1,
        members=(member,),
        blocked_candidates=(),
        contract_registry_version="identity-v1",
        exclusion_ledger_version="empty-v1",
        candle_dataset_hash=_hash(f"candles:{day.isoformat()}"),
    )


def _unverified_registry() -> ContractRegistry:
    onboard = datetime(2023, 1, 1, tzinfo=UTC)
    return ContractRegistry(
        registry_version="draft-rules",
        entries=(
            ContractRegistryEntry(
                symbol="AAAUSDT",
                base_asset="AAA",
                quote_asset="USDT",
                margin_asset="USDT",
                contract_type="PERPETUAL",
                status="TRADING",
                onboard_date=onboard,
                derived_first_candle_at=onboard,
                inferred_delisted_at=None,
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                max_qty=Decimal("10000"),
                min_notional=Decimal("5"),
                effective_from=onboard,
                effective_to=None,
                source_ref="fixture:rule",
                source_snapshot_hash=_hash("rule"),
                derivation_method="FIXTURE",
                reviewed_by="PENDING",
                verification_status=RegistryVerification.UNVERIFIED,
                confidence=EvidenceConfidence.MEDIUM,
            ),
        ),
    )


def test_exact_global_split_boundaries_and_locked_role_guard() -> None:
    manifest = build_research_split(
        research_start=date(2023, 8, 1),
        research_end_exclusive=date(2026, 8, 1),
        universe_batch_run_version=_hash("run"),
        daily_snapshot_hash=_hash("snapshots"),
    )

    assert [
        (segment.role, segment.start, segment.end_exclusive, segment.day_count)
        for segment in manifest.segments
    ] == [
        (DatasetRole.DEV, date(2023, 8, 1), date(2025, 1, 30), 548),
        (DatasetRole.VALIDATION, date(2025, 1, 30), date(2025, 10, 31), 274),
        (DatasetRole.LOCKED_TEST, date(2025, 10, 31), date(2026, 8, 1), 274),
    ]
    assert role_for_date(manifest, date(2025, 1, 29)) is DatasetRole.DEV
    assert role_for_date(manifest, date(2025, 1, 30)) is DatasetRole.VALIDATION
    with pytest.raises(ResearchSplitError, match="one-time gate"):
        require_research_role(DatasetRole.LOCKED_TEST)
    for role in (DatasetRole.DEV, DatasetRole.VALIDATION):
        require_research_role(role)
    assert role_for_date(manifest, date(2025, 10, 31)) is DatasetRole.LOCKED_TEST
    with pytest.raises(ResearchSplitError, match="outside"):
        role_for_date(manifest, date(2026, 8, 1))


def test_audit_blocks_all_roles_without_verified_rule_member_days() -> None:
    start = date(2024, 1, 1)
    snapshots = tuple(_snapshot(start + timedelta(days=offset)) for offset in range(4))
    sequence_hash = snapshot_sequence_hash(snapshots)
    split = build_research_split(
        research_start=start,
        research_end_exclusive=start + timedelta(days=4),
        universe_batch_run_version=_hash("run"),
        daily_snapshot_hash=sequence_hash,
    )

    report = audit_research_inputs(
        split=split,
        snapshots=snapshots,
        registry=_unverified_registry(),
    )

    assert report.overall_readiness is ResearchReadiness.BLOCKED
    assert report.locked_test_consumed is False
    assert [role.expected_day_count for role in report.roles] == [2, 1, 1]
    assert [role.rule_eligible_member_day_count for role in report.roles] == [0, 0, 0]
    assert all(
        role.rule_reason_counts == {"CONTRACT_RULE_UNVERIFIED": role.member_day_count}
        for role in report.roles
    )
    assert audit_research_inputs(
        split=split,
        snapshots=tuple(reversed(snapshots)),
        registry=_unverified_registry(),
    ) == report
    for invalid in (snapshots[:-1], (*snapshots[:-1], snapshots[0])):
        with pytest.raises(ResearchSplitError, match="exactly cover"):
            audit_research_inputs(
                split=split, snapshots=invalid, registry=_unverified_registry()
            )


@pytest.mark.parametrize("day_count", [-4, 0, 1, 5])
def test_invalid_or_ambiguous_research_interval_is_rejected(day_count: int) -> None:
    start = date(2024, 1, 1)
    with pytest.raises(ResearchSplitError, match="positive multiple of 4"):
        build_research_split(
            research_start=start,
            research_end_exclusive=start + timedelta(days=day_count),
            universe_batch_run_version=_hash("run"),
            daily_snapshot_hash=_hash("snapshots"),
        )


def test_split_artifact_is_idempotent_and_cannot_be_overwritten(tmp_path: Path) -> None:
    split = build_research_split(
        research_start=date(2023, 8, 1),
        research_end_exclusive=date(2026, 8, 1),
        universe_batch_run_version=_hash("run"),
        daily_snapshot_hash=_hash("snapshots"),
    )
    path = write_research_split(split, tmp_path)
    assert write_research_split(split, tmp_path) == path
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchSplitError, match="changed"):
        write_research_split(split, tmp_path)
