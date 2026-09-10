"""Immutable P8 contracts for historical symbols and daily Universe snapshots."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.domain.symbols import normalize_asset, normalize_symbol


class Exchange(StrEnum):
    """Exchange scope supported by V0.1."""

    BINANCE_USDM = "BINANCE_USDM"


class RegistryVerification(StrEnum):
    """Human-reviewed lifecycle evidence state."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class EvidenceConfidence(StrEnum):
    """Confidence attached to the recorded evidence, not a trading score."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


APPROXIMATE_TICK_SIZE_WARNING = "APPROXIMATE_HISTORICAL_TICK_SIZE"


class ExclusionCategory(StrEnum):
    """Frozen historical exclusion categories from the P0 data contract."""

    STABLE_SWAP = "STABLE_SWAP"
    INDEX = "INDEX"
    LEVERAGED_TOKEN = "LEVERAGED_TOKEN"
    TRADFI = "TRADFI"
    DATA_QUALITY = "DATA_QUALITY"


class UniverseBlockReason(StrEnum):
    """Stable reason codes for a symbol-day that must fail closed."""

    CHECKSUM_FAILED = "CHECKSUM_FAILED"
    ARCHIVE_INVALID = "ARCHIVE_INVALID"
    CANDLE_GAP = "CANDLE_GAP"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    CANDLE_INVALID = "CANDLE_INVALID"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    CONTRACT_UNVERIFIED = "CONTRACT_UNVERIFIED"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    EXCLUDED = "EXCLUDED"
    SYMBOL_INELIGIBLE = "SYMBOL_INELIGIBLE"


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _normalized_symbol(value: str) -> str:
    return normalize_symbol(value)


def _normalized_asset(value: str) -> str:
    return normalize_asset(value)


def _nonempty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _sha256(value: str) -> str:
    normalized = value.strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError("sha256 must contain exactly 64 lowercase hexadecimal characters")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value


class ContractRegistryEntry(BaseModel):
    """One versioned contract-rule interval with auditable evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["contract-rule/0.1.0"] = "contract-rule/0.1.0"
    exchange: Exchange = Exchange.BINANCE_USDM
    symbol: str
    base_asset: str
    quote_asset: str
    margin_asset: str
    contract_type: str
    status: str
    onboard_date: datetime | None
    derived_first_candle_at: datetime
    inferred_delisted_at: datetime | None
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None
    effective_from: datetime
    effective_to: datetime | None
    source_ref: str
    source_snapshot_hash: str
    derivation_method: str
    reviewed_by: str
    verification_status: RegistryVerification
    confidence: EvidenceConfidence

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)

    @field_validator("base_asset", "quote_asset", "margin_asset", mode="before")
    @classmethod
    def validate_asset(cls, value: str) -> str:
        return _normalized_asset(value)

    @field_validator("contract_type", "status", mode="before")
    @classmethod
    def normalize_exchange_value(cls, value: str) -> str:
        return _nonempty(value, "exchange value").upper()

    @field_validator("source_ref", "derivation_method", "reviewed_by")
    @classmethod
    def validate_evidence_text(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "evidence")
        return _nonempty(value, str(field_name))

    @field_validator("source_snapshot_hash")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        return _sha256(value)

    @field_validator(
        "onboard_date",
        "derived_first_candle_at",
        "inferred_delisted_at",
        "effective_from",
        "effective_to",
    )
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.quote_asset != "USDT":
            raise ValueError("quote_asset must be USDT")
        if self.margin_asset != "USDT":
            raise ValueError("margin_asset must be USDT")
        if self.contract_type != "PERPETUAL":
            raise ValueError("contract_type must be PERPETUAL")
        for name, required_value in (
            ("tick_size", self.tick_size),
            ("step_size", self.step_size),
        ):
            if not required_value.is_finite() or required_value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name, optional_value in (
            ("min_qty", self.min_qty),
            ("max_qty", self.max_qty),
            ("min_notional", self.min_notional),
        ):
            if optional_value is not None and (
                not optional_value.is_finite() or optional_value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive when provided")
        if (
            self.min_qty is not None
            and self.max_qty is not None
            and self.min_qty > self.max_qty
        ):
            raise ValueError("min_qty must not exceed max_qty")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be later than effective_from")
        if (
            self.onboard_date is not None
            and self.derived_first_candle_at < self.onboard_date
        ):
            raise ValueError("derived_first_candle_at must not precede onboard_date")
        if (
            self.inferred_delisted_at is not None
            and self.inferred_delisted_at <= self.derived_first_candle_at
        ):
            raise ValueError("inferred_delisted_at must follow derived_first_candle_at")
        return self

    @property
    def eligible_for_locked_research(self) -> bool:
        """Whether lifecycle evidence passed review; other P8.3 gates still apply."""

        return self.verification_status is RegistryVerification.VERIFIED

    @property
    def eligible_for_dev_research(self) -> bool:
        """Allow traceable medium-confidence rules in DEV without calling them verified."""

        return self.eligible_for_locked_research or (
            self.verification_status is RegistryVerification.UNVERIFIED
            and self.confidence in (EvidenceConfidence.HIGH, EvidenceConfidence.MEDIUM)
        )


class ContractRegistry(BaseModel):
    """Canonical immutable collection of non-overlapping contract-rule intervals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["contract-registry/0.1.0"] = "contract-registry/0.1.0"
    registry_version: str
    entries: tuple[ContractRegistryEntry, ...]

    @field_validator("registry_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _nonempty(value, "registry_version")

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        keys = tuple((entry.symbol, entry.effective_from) for entry in self.entries)
        if keys != tuple(sorted(keys)):
            raise ValueError("registry entries must use canonical order by symbol/effective_from")
        for previous, current in zip(self.entries, self.entries[1:], strict=False):
            if previous.symbol != current.symbol:
                continue
            if previous.effective_to is None or previous.effective_to > current.effective_from:
                raise ValueError("contract registry intervals overlap for one symbol")
        return self


class ContractIdentityLifecycleEntry(BaseModel):
    """Verified identity/lifecycle interval without asserting historical filters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["contract-identity-lifecycle/0.1.0"] = (
        "contract-identity-lifecycle/0.1.0"
    )
    symbol: str
    base_asset: str
    quote_asset: str
    margin_asset: str
    contract_type: str
    status: Literal["TRADING"] = "TRADING"
    onboard_date: datetime
    derived_first_candle_at: datetime
    effective_from: datetime
    effective_to: datetime | None
    source_ref: str
    source_snapshot_hash: str
    reviewed_by: str
    verification_status: RegistryVerification
    confidence: EvidenceConfidence

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)

    @field_validator("base_asset", "quote_asset", "margin_asset", mode="before")
    @classmethod
    def validate_asset(cls, value: str) -> str:
        return _normalized_asset(value)

    @field_validator("contract_type", mode="before")
    @classmethod
    def validate_contract_type(cls, value: str) -> str:
        return _nonempty(value, "contract_type").upper()

    @field_validator("source_ref", "reviewed_by")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        return _nonempty(value, str(getattr(info, "field_name", "evidence")))

    @field_validator("source_snapshot_hash")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        return _sha256(value)

    @field_validator(
        "onboard_date", "derived_first_candle_at", "effective_from", "effective_to"
    )
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if (
            self.contract_type != "PERPETUAL"
            or self.quote_asset != "USDT"
            or self.margin_asset != "USDT"
        ):
            raise ValueError("identity lifecycle must be PERPETUAL USDT quote/margin")
        if self.effective_from != self.onboard_date:
            raise ValueError("effective_from must equal official onboard_date")
        if self.derived_first_candle_at < self.onboard_date:
            raise ValueError("derived_first_candle_at must not precede onboard_date")
        if self.effective_to is not None and self.effective_to <= self.derived_first_candle_at:
            raise ValueError("effective_to must follow the first verified candle")
        return self

    @property
    def eligible_for_locked_research(self) -> bool:
        return self.verification_status is RegistryVerification.VERIFIED


class ContractIdentityRegistry(BaseModel):
    """Canonical identity/lifecycle evidence used only by Universe selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["contract-identity-registry/0.1.0"] = (
        "contract-identity-registry/0.1.0"
    )
    registry_version: str
    entries: tuple[ContractIdentityLifecycleEntry, ...]

    @field_validator("registry_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _nonempty(value, "registry_version")

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        keys = tuple((entry.symbol, entry.effective_from) for entry in self.entries)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("identity entries must be unique and canonical")
        return self


class ExclusionLedgerEntry(BaseModel):
    """One exact-symbol, time-bounded and reviewed exclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    category: ExclusionCategory
    effective_from: datetime
    effective_to: datetime | None
    reason: str
    evidence_ref: str
    reviewed_by: str
    ledger_version: str

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)

    @field_validator("effective_from", "effective_to")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("reason", "evidence_ref", "reviewed_by", "ledger_version")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "ledger field")
        return _nonempty(value, str(field_name))

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be later than effective_from")
        return self


class ExclusionLedger(BaseModel):
    """Canonical immutable exclusion ledger; no fuzzy runtime classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["exclusion-ledger/0.1.0"] = "exclusion-ledger/0.1.0"
    ledger_version: str
    entries: tuple[ExclusionLedgerEntry, ...]

    @field_validator("ledger_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _nonempty(value, "ledger_version")

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        if any(entry.ledger_version != self.ledger_version for entry in self.entries):
            raise ValueError("entry ledger_version must match ledger ledger_version")
        keys = tuple(
            (entry.symbol, entry.effective_from, entry.category.value, entry.evidence_ref)
            for entry in self.entries
        )
        if keys != tuple(sorted(keys)):
            raise ValueError("ledger entries must use canonical order")
        if len(set(keys)) != len(keys):
            raise ValueError("ledger entries must be unique")
        return self


class UniverseMember(BaseModel):
    """One ranked member and its frozen eligibility evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    rank: int = Field(ge=1, le=30)
    rolling_quote_volume_24h: Decimal
    forced: bool
    eligibility_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)

    @field_validator("eligibility_refs")
    @classmethod
    def validate_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_nonempty(value, "eligibility_ref") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("eligibility_refs must be unique")
        return normalized

    @field_validator("rolling_quote_volume_24h")
    @classmethod
    def validate_volume(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("rolling_quote_volume_24h must be finite and non-negative")
        return value


class UniverseBlockedCandidate(BaseModel):
    """One candidate excluded from a symbol-day because a gate failed closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    reason_codes: tuple[UniverseBlockReason, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _normalized_symbol(value)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(
        cls,
        values: tuple[UniverseBlockReason, ...],
    ) -> tuple[UniverseBlockReason, ...]:
        if len(set(values)) != len(values):
            raise ValueError("reason_codes must be unique")
        if values != tuple(sorted(values, key=lambda reason: reason.value)):
            raise ValueError("reason_codes must use canonical order")
        return values

    @field_validator("evidence_refs")
    @classmethod
    def validate_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_nonempty(value, "evidence_ref") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_refs must be unique")
        return normalized


class UniverseSnapshot(BaseModel):
    """One immutable daily Universe result; selection itself belongs to P8.3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["universe-snapshot/0.1.0"] = "universe-snapshot/0.1.0"
    universe_version: str
    selection_algorithm_version: str
    selected_at: datetime
    effective_from: datetime
    effective_to: datetime
    ranking_window_start: datetime
    ranking_window_end: datetime
    member_count: int = Field(ge=0, le=30)
    members: tuple[UniverseMember, ...]
    blocked_candidates: tuple[UniverseBlockedCandidate, ...]
    contract_registry_version: str
    exclusion_ledger_version: str
    candle_dataset_hash: str

    @field_validator("universe_version", "candle_dataset_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _sha256(value)

    @field_validator(
        "selection_algorithm_version",
        "contract_registry_version",
        "exclusion_ledger_version",
    )
    @classmethod
    def validate_version(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "version")
        return _nonempty(value, str(field_name))

    @field_validator(
        "selected_at",
        "effective_from",
        "effective_to",
        "ranking_window_start",
        "ranking_window_end",
    )
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        expected_midnight = self.selected_at.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if self.selected_at != expected_midnight + timedelta(minutes=5):
            raise ValueError("selected_at must be exactly 00:05 UTC")
        if self.ranking_window_end != expected_midnight:
            raise ValueError("ranking_window_end must be exactly 00:00 UTC")
        if self.ranking_window_end - self.ranking_window_start != timedelta(days=1):
            raise ValueError("ranking window must cover exactly 24 hours")
        if self.effective_from != expected_midnight + timedelta(minutes=15):
            raise ValueError("effective_from must be exactly 00:15 UTC")
        if self.effective_to != self.effective_from + timedelta(days=1):
            raise ValueError("effective_to must be 24 hours after effective_from")
        if self.member_count != len(self.members):
            raise ValueError("member_count must equal the number of members")

        ranks = tuple(member.rank for member in self.members)
        expected_ranks = tuple(range(1, len(self.members) + 1))
        if ranks != expected_ranks:
            raise ValueError("member ranks must be ordered and contiguous from 1")
        member_symbols = tuple(member.symbol for member in self.members)
        if len(set(member_symbols)) != len(member_symbols):
            raise ValueError("member symbols must be unique")

        blocked_symbols = tuple(candidate.symbol for candidate in self.blocked_candidates)
        if blocked_symbols != tuple(sorted(blocked_symbols)):
            raise ValueError("blocked candidates must use canonical symbol order")
        if len(set(blocked_symbols)) != len(blocked_symbols):
            raise ValueError("blocked candidate symbols must be unique")
        if set(member_symbols).intersection(blocked_symbols):
            raise ValueError("a symbol cannot be both selected and blocked")
        return self
