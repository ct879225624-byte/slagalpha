"""Fail-closed historical registry drafts from current official metadata."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from slagalpha.data.exchange_info import ExchangeContract
from slagalpha.data.klines import ArchiveNormalizationManifest
from slagalpha.domain.universe import (
    ContractIdentityLifecycleEntry,
    ContractIdentityRegistry,
    ContractRegistry,
    ContractRegistryEntry,
    EvidenceConfidence,
    RegistryVerification,
)


class RegistryDraftError(RuntimeError):
    """Raised when registry draft evidence is malformed or contradictory."""


class RegistryDraftReport(BaseModel):
    """Summary proving no draft entry was silently locked for research."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["registry-draft-report/0.1.0"] = (
        "registry-draft-report/0.1.0"
    )
    exchange_info_hash: str
    registry_version: str
    input_contract_count: int = Field(ge=0)
    draft_entry_count: int = Field(ge=0)
    locked_entry_count: int = Field(ge=0)
    skipped_ineligible_symbols: tuple[str, ...]
    skipped_without_candle_evidence_symbols: tuple[str, ...]
    report_hash: str

    @field_validator("exchange_info_hash", "report_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("registry draft hashes must be SHA-256")
        return normalized


class IdentityRegistryBuildReport(BaseModel):
    """Content-addressed result of the user-approved identity-only policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["identity-registry-build/0.1.0"] = (
        "identity-registry-build/0.1.0"
    )
    exchange_info_hash: str
    registry_version: str
    verified_entry_count: int = Field(ge=0)
    skipped_symbols: tuple[str, ...]
    report_hash: str

    @field_validator("exchange_info_hash", "report_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("identity registry hashes must be SHA-256")
        return normalized


def _canonical_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _ceil_fifteen_minutes(value: datetime) -> datetime:
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = value - midnight
    interval = timedelta(minutes=15)
    steps, remainder = divmod(elapsed, interval)
    return midnight + (steps + (1 if remainder else 0)) * interval


def _first_candle_at_or_after(
    onboard_date: datetime,
    manifests: tuple[ArchiveNormalizationManifest, ...],
) -> datetime | None:
    aligned = _ceil_fifteen_minutes(onboard_date)
    candidates = []
    for manifest in manifests:
        if manifest.interval != "15m" or manifest.last_open_time < onboard_date:
            continue
        candidate = max(manifest.first_open_time, aligned)
        if candidate <= manifest.last_open_time:
            candidates.append(candidate)
    return min(candidates) if candidates else None


def build_registry_draft(
    *,
    contracts: tuple[ExchangeContract, ...],
    fifteen_minute_evidence: Mapping[
        str, tuple[ArchiveNormalizationManifest, ...]
    ],
    exchange_info_hash: str,
) -> tuple[ContractRegistry, RegistryDraftReport]:
    """Create UNVERIFIED entries; current filters are never asserted as historical."""

    ineligible = []
    without_evidence = []
    entries = []
    for contract in sorted(contracts, key=lambda item: item.symbol):
        eligible_identity = (
            contract.contract_type == "PERPETUAL"
            and contract.quote_asset == "USDT"
            and contract.margin_asset == "USDT"
            and contract.status in {"TRADING", "SETTLING"}
            and contract.onboard_date is not None
        )
        if not eligible_identity:
            ineligible.append(contract.symbol)
            continue
        evidence = tuple(fifteen_minute_evidence.get(contract.symbol, ()))
        assert contract.onboard_date is not None
        derived_first = _first_candle_at_or_after(contract.onboard_date, evidence)
        if derived_first is None:
            without_evidence.append(contract.symbol)
            continue
        effective_to = (
            contract.delivery_date if contract.status == "SETTLING" else None
        )
        if effective_to is not None and effective_to <= derived_first:
            without_evidence.append(contract.symbol)
            continue
        entries.append(
            ContractRegistryEntry(
                symbol=contract.symbol,
                base_asset=contract.base_asset,
                quote_asset=contract.quote_asset,
                margin_asset=contract.margin_asset,
                contract_type=contract.contract_type,
                status="TRADING",
                onboard_date=contract.onboard_date,
                derived_first_candle_at=derived_first,
                inferred_delisted_at=effective_to,
                tick_size=contract.tick_size,
                step_size=contract.step_size,
                min_qty=contract.min_qty,
                max_qty=contract.max_qty,
                min_notional=contract.min_notional,
                effective_from=contract.onboard_date,
                effective_to=effective_to,
                source_ref=f"exchangeInfo:{exchange_info_hash}",
                source_snapshot_hash=exchange_info_hash,
                derivation_method=(
                    "OFFICIAL_CURRENT_IDENTITY_AND_ONBOARD; CURRENT_FILTERS_NOT_"
                    "ASSUMED_HISTORICAL"
                ),
                reviewed_by="PENDING_HUMAN_REVIEW",
                verification_status=RegistryVerification.UNVERIFIED,
                confidence=EvidenceConfidence.MEDIUM,
            )
        )

    canonical_entries = tuple(
        sorted(entries, key=lambda item: (item.symbol, item.effective_from))
    )
    registry_hash = _canonical_sha256(
        [entry.model_dump(mode="json") for entry in canonical_entries]
    )
    registry_version = f"registry-draft-{registry_hash}"
    registry = ContractRegistry(
        registry_version=registry_version,
        entries=canonical_entries,
    )
    report_payload = {
        "exchange_info_hash": exchange_info_hash,
        "registry_version": registry_version,
        "input_contract_count": len(contracts),
        "draft_entry_count": len(canonical_entries),
        "locked_entry_count": 0,
        "skipped_ineligible_symbols": tuple(sorted(set(ineligible))),
        "skipped_without_candle_evidence_symbols": tuple(
            sorted(set(without_evidence))
        ),
    }
    report = RegistryDraftReport(
        exchange_info_hash=exchange_info_hash,
        registry_version=registry_version,
        input_contract_count=len(contracts),
        draft_entry_count=len(canonical_entries),
        locked_entry_count=0,
        skipped_ineligible_symbols=tuple(sorted(set(ineligible))),
        skipped_without_candle_evidence_symbols=tuple(
            sorted(set(without_evidence))
        ),
        report_hash=_canonical_sha256(report_payload),
    )
    return registry, report


def build_identity_registry(
    *,
    contracts: tuple[ExchangeContract, ...],
    fifteen_minute_evidence: Mapping[
        str, tuple[ArchiveNormalizationManifest, ...]
    ],
    exchange_info_hash: str,
) -> tuple[ContractIdentityRegistry, IdentityRegistryBuildReport]:
    """Build verified identity intervals without carrying current filter values."""

    entries = []
    skipped = []
    for contract in sorted(contracts, key=lambda item: item.symbol):
        eligible = (
            contract.contract_type == "PERPETUAL"
            and contract.quote_asset == "USDT"
            and contract.margin_asset == "USDT"
            and contract.status in {"TRADING", "SETTLING"}
            and contract.onboard_date is not None
        )
        if not eligible:
            continue
        assert contract.onboard_date is not None
        derived_first = _first_candle_at_or_after(
            contract.onboard_date,
            tuple(fifteen_minute_evidence.get(contract.symbol, ())),
        )
        effective_to = contract.delivery_date if contract.status == "SETTLING" else None
        if derived_first is None or (
            effective_to is not None and effective_to <= derived_first
        ):
            skipped.append(contract.symbol)
            continue
        entries.append(
            ContractIdentityLifecycleEntry(
                symbol=contract.symbol,
                base_asset=contract.base_asset,
                quote_asset=contract.quote_asset,
                margin_asset=contract.margin_asset,
                contract_type=contract.contract_type,
                onboard_date=contract.onboard_date,
                derived_first_candle_at=derived_first,
                effective_from=contract.onboard_date,
                effective_to=effective_to,
                source_ref=f"exchangeInfo:{exchange_info_hash}",
                source_snapshot_hash=exchange_info_hash,
                reviewed_by="USER_CONFIRMED_IDENTITY_RULE_SPLIT_2026-08-30",
                verification_status=RegistryVerification.VERIFIED,
                confidence=EvidenceConfidence.HIGH,
            )
        )
    canonical = tuple(sorted(entries, key=lambda item: (item.symbol, item.effective_from)))
    content_hash = _canonical_sha256(
        [entry.model_dump(mode="json") for entry in canonical]
    )
    registry = ContractIdentityRegistry(
        registry_version=f"identity-registry-{content_hash}",
        entries=canonical,
    )
    payload = {
        "exchange_info_hash": exchange_info_hash,
        "registry_version": registry.registry_version,
        "verified_entry_count": len(canonical),
        "skipped_symbols": tuple(sorted(set(skipped))),
    }
    report = IdentityRegistryBuildReport(
        exchange_info_hash=exchange_info_hash,
        registry_version=registry.registry_version,
        verified_entry_count=len(canonical),
        skipped_symbols=tuple(sorted(set(skipped))),
        report_hash=_canonical_sha256(payload),
    )
    return registry, report


def write_identity_registry(
    registry: ContractIdentityRegistry,
    report: IdentityRegistryBuildReport,
    manifests_dir: Path,
) -> tuple[Path, Path]:
    """Persist the verified identity-only registry and build report."""

    registry_hash = registry.registry_version.removeprefix("identity-registry-")
    outputs = (
        manifests_dir / "contract_identity_registry" / f"{registry_hash}.json",
        manifests_dir / "identity_registry_build" / f"{report.report_hash}.json",
    )
    models: tuple[BaseModel, ...] = (registry, report)
    for destination, model in zip(outputs, models, strict=True):
        content = (
            json.dumps(
                model.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if destination.exists():
            if destination.read_bytes() != content:
                raise RegistryDraftError(
                    f"existing content-addressed identity artifact changed: {destination}"
                )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        try:
            temporary.write_bytes(content)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return outputs


def write_registry_draft(
    registry: ContractRegistry,
    report: RegistryDraftReport,
    manifests_dir: Path,
) -> tuple[Path, Path]:
    """Persist the immutable draft and its fail-closed summary."""

    registry_hash = registry.registry_version.removeprefix("registry-draft-")
    paths_and_models: tuple[tuple[Path, BaseModel], ...] = (
        (
            manifests_dir / "contract_registry" / f"{registry_hash}.json",
            registry,
        ),
        (
            manifests_dir / "registry_draft" / f"{report.report_hash}.json",
            report,
        ),
    )
    outputs = []
    for destination, model in paths_and_models:
        content = (
            json.dumps(
                model.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if destination.exists():
            if destination.read_bytes() != content:
                raise RegistryDraftError(
                    f"existing content-addressed registry artifact changed: {destination}"
                )
            outputs.append(destination)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        try:
            temporary.write_bytes(content)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        outputs.append(destination)
    return outputs[0], outputs[1]
