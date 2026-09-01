"""Auditable current Binance USD-M exchangeInfo snapshots and coverage reports."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.domain.symbols import normalize_asset, normalize_symbol

EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_BAN_UNTIL_PATTERN = re.compile(r"banned until\s+(\d+)", re.IGNORECASE)


class ExchangeInfoError(RuntimeError):
    """Raised when current public exchange metadata cannot be trusted."""


class ExchangeInfoRateLimitedError(ExchangeInfoError):
    """Raised without retrying when Binance reports an IP rate ban."""

    def __init__(self, message: str, banned_until: datetime | None) -> None:
        super().__init__(message)
        self.banned_until = banned_until


class ExchangeContract(BaseModel):
    """Current contract metadata using exchange filters as price/quantity authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    pair: str
    contract_type: str
    status: str
    onboard_date: datetime | None
    delivery_date: datetime | None
    base_asset: str
    quote_asset: str
    margin_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None

    @field_validator("symbol", "pair", mode="before")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("base_asset", "quote_asset", "margin_asset", mode="before")
    @classmethod
    def validate_asset(cls, value: str) -> str:
        return normalize_asset(value)

    @field_validator("contract_type", "status", mode="before")
    @classmethod
    def validate_exchange_text(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("exchange metadata value must not be empty")
        return normalized

    @field_validator("onboard_date", "delivery_date")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("contract timestamp must use UTC")
        return value

    @model_validator(mode="after")
    def validate_filters(self) -> Self:
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
        if self.min_qty is not None and self.max_qty is not None and self.min_qty > self.max_qty:
            raise ValueError("min_qty must not exceed max_qty")
        return self


class ExchangeInfoSnapshot(BaseModel):
    """One content-addressed current exchangeInfo response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["exchange-info-snapshot/0.1.0"] = (
        "exchange-info-snapshot/0.1.0"
    )
    source_url: Literal["https://fapi.binance.com/fapi/v1/exchangeInfo"] = (
        "https://fapi.binance.com/fapi/v1/exchangeInfo"
    )
    raw_sha256: str
    raw_payload: bytes = Field(exclude=True, repr=False)
    fetched_at: datetime
    server_time: datetime
    contracts: tuple[ExchangeContract, ...]

    @field_validator("raw_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("raw_sha256 must be a SHA-256")
        return normalized

    @field_validator("fetched_at", "server_time")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("snapshot timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if hashlib.sha256(self.raw_payload).hexdigest() != self.raw_sha256:
            raise ValueError("raw_sha256 does not match raw_payload")
        symbols = tuple(contract.symbol for contract in self.contracts)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("contracts must have unique symbols in canonical order")
        return self

    @property
    def current_usdt_perpetual_symbols(self) -> tuple[str, ...]:
        """Current eligible contract identities; not historical status evidence."""

        return tuple(
            contract.symbol
            for contract in self.contracts
            if contract.contract_type == "PERPETUAL"
            and contract.status == "TRADING"
            and contract.quote_asset == "USDT"
            and contract.margin_asset == "USDT"
        )


class RegistryCoverageReport(BaseModel):
    """Set-level audit between raw archive prefixes and current contract metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["registry-coverage/0.1.0"] = "registry-coverage/0.1.0"
    exchange_info_hash: str
    archive_listing_hash: str
    raw_archive_symbol_count: int = Field(ge=0)
    current_usdt_perpetual_count: int = Field(ge=0)
    current_with_archive: tuple[str, ...]
    current_without_archive: tuple[str, ...]
    archive_only: tuple[str, ...]
    coverage_hash: str

    @field_validator("exchange_info_hash", "coverage_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("coverage hashes must be SHA-256")
        return normalized

    @field_validator("archive_listing_hash")
    @classmethod
    def validate_listing_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "unknown":
            return normalized
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("archive_listing_hash must be SHA-256 or unknown")
        return normalized

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        for values in (
            self.current_with_archive,
            self.current_without_archive,
            self.archive_only,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("coverage symbol sets must be unique and canonical")
        if set(self.current_with_archive).intersection(self.current_without_archive):
            raise ValueError("current coverage sets must be disjoint")
        if self.current_usdt_perpetual_count != len(self.current_with_archive) + len(
            self.current_without_archive
        ):
            raise ValueError("current_usdt_perpetual_count does not match symbol sets")
        expected_hash = _coverage_hash(
            exchange_info_hash=self.exchange_info_hash,
            archive_listing_hash=self.archive_listing_hash,
            raw_archive_symbol_count=self.raw_archive_symbol_count,
            current_with_archive=self.current_with_archive,
            current_without_archive=self.current_without_archive,
            archive_only=self.archive_only,
        )
        if self.coverage_hash != expected_hash:
            raise ValueError("coverage_hash does not match report content")
        return self


def _milliseconds(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExchangeInfoError(f"{field} must be Unix milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise ExchangeInfoError(f"{field} is outside supported timestamp range") from error


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ExchangeInfoError(f"{field} must be a decimal") from error
    if not parsed.is_finite():
        raise ExchangeInfoError(f"{field} must be finite")
    return parsed


def _filter_map(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_filters = contract.get("filters")
    if not isinstance(raw_filters, list):
        raise ExchangeInfoError("contract filters must be an array")
    result: dict[str, dict[str, Any]] = {}
    for item in raw_filters:
        if not isinstance(item, dict) or not isinstance(item.get("filterType"), str):
            raise ExchangeInfoError("contract filter must have filterType")
        filter_type = item["filterType"]
        if filter_type in result:
            raise ExchangeInfoError(f"duplicate contract filter {filter_type}")
        result[filter_type] = item
    return result


def _required_string(values: dict[str, Any], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str):
        raise ExchangeInfoError(f"{field} must be a string")
    return value


def parse_exchange_info(
    raw_payload: bytes,
    *,
    fetched_at: datetime | None = None,
) -> ExchangeInfoSnapshot:
    """Parse a complete exchangeInfo response using filters, never precision fields."""

    try:
        payload = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExchangeInfoError("exchangeInfo response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ExchangeInfoError("exchangeInfo root must be an object")
    if payload.get("code") == -1003:
        raise _rate_limit_error(payload)
    raw_contracts = payload.get("symbols")
    if not isinstance(raw_contracts, list):
        raise ExchangeInfoError("exchangeInfo symbols must be an array")

    contracts = []
    for raw_contract in raw_contracts:
        if not isinstance(raw_contract, dict):
            raise ExchangeInfoError("exchangeInfo symbol entry must be an object")
        filters = _filter_map(raw_contract)
        price_filter = filters.get("PRICE_FILTER")
        lot_filter = filters.get("LOT_SIZE")
        if price_filter is None or lot_filter is None:
            raise ExchangeInfoError("contract lacks PRICE_FILTER or LOT_SIZE")
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
        contracts.append(
            ExchangeContract(
                symbol=_required_string(raw_contract, "symbol"),
                pair=(
                    _required_string(raw_contract, "pair")
                    if "pair" in raw_contract
                    else _required_string(raw_contract, "symbol")
                ),
                contract_type=_required_string(raw_contract, "contractType"),
                status=_required_string(raw_contract, "status"),
                onboard_date=_milliseconds(
                    raw_contract.get("onboardDate"),
                    "onboardDate",
                    optional=True,
                ),
                delivery_date=_milliseconds(
                    raw_contract.get("deliveryDate"),
                    "deliveryDate",
                    optional=True,
                ),
                base_asset=_required_string(raw_contract, "baseAsset"),
                quote_asset=_required_string(raw_contract, "quoteAsset"),
                margin_asset=_required_string(raw_contract, "marginAsset"),
                tick_size=_decimal(price_filter.get("tickSize"), "tickSize"),
                step_size=_decimal(lot_filter.get("stepSize"), "stepSize"),
                min_qty=(
                    _decimal(lot_filter["minQty"], "minQty")
                    if lot_filter.get("minQty") is not None
                    else None
                ),
                max_qty=(
                    _decimal(lot_filter["maxQty"], "maxQty")
                    if lot_filter.get("maxQty") is not None
                    else None
                ),
                min_notional=(
                    _decimal(notional_filter["notional"], "minNotional")
                    if notional_filter is not None
                    and notional_filter.get("notional") is not None
                    else None
                ),
            )
        )
    server_time = _milliseconds(payload.get("serverTime"), "serverTime")
    assert server_time is not None
    return ExchangeInfoSnapshot(
        raw_sha256=hashlib.sha256(raw_payload).hexdigest(),
        raw_payload=raw_payload,
        fetched_at=fetched_at or datetime.now(UTC),
        server_time=server_time,
        contracts=tuple(sorted(contracts, key=lambda contract: contract.symbol)),
    )


def _rate_limit_error(payload: dict[str, Any]) -> ExchangeInfoRateLimitedError:
    message = str(payload.get("msg", "Binance exchangeInfo rate limited"))
    match = _BAN_UNTIL_PATTERN.search(message)
    banned_until = (
        datetime.fromtimestamp(int(match.group(1)) / 1000, tz=UTC) if match else None
    )
    return ExchangeInfoRateLimitedError(message, banned_until)


@contextmanager
def _managed_client(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "slagalpha/0.1.0"},
    ) as owned:
        yield owned


def fetch_exchange_info(
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> ExchangeInfoSnapshot:
    """Fetch one current public snapshot with bounded transport retries."""

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    with _managed_client(client) as active_client:
        for attempt in range(1, attempts + 1):
            try:
                response = active_client.get(EXCHANGE_INFO_URL)
                if response.status_code == 429:
                    try:
                        body = response.json()
                    except json.JSONDecodeError:
                        body = {"msg": response.text}
                    raise _rate_limit_error(body)
                response.raise_for_status()
                try:
                    body = response.json()
                except json.JSONDecodeError:
                    body = None
                if isinstance(body, dict) and body.get("code") == -1003:
                    raise _rate_limit_error(body)
                return parse_exchange_info(response.content)
            except ExchangeInfoRateLimitedError:
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                retryable = isinstance(error, httpx.TransportError) or (
                    error.response.status_code >= 500
                )
                if not retryable or attempt == attempts:
                    raise ExchangeInfoError(f"failed to fetch exchangeInfo: {error}") from error
                sleep(0.25 * (2 ** (attempt - 1)))
    raise ExchangeInfoError("failed to fetch exchangeInfo")


def _coverage_hash(
    *,
    exchange_info_hash: str,
    archive_listing_hash: str,
    raw_archive_symbol_count: int,
    current_with_archive: tuple[str, ...],
    current_without_archive: tuple[str, ...],
    archive_only: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "exchange_info_hash": exchange_info_hash,
            "archive_listing_hash": archive_listing_hash,
            "raw_archive_symbol_count": raw_archive_symbol_count,
            "current_with_archive": current_with_archive,
            "current_without_archive": current_without_archive,
            "archive_only": archive_only,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def audit_registry_coverage(
    *,
    archive_symbols: tuple[str, ...],
    exchange_info: ExchangeInfoSnapshot,
    archive_listing_hash: str = "unknown",
) -> RegistryCoverageReport:
    """Compare sets without treating archive-only symbols as verified contracts."""

    archives = tuple(sorted({normalize_symbol(symbol) for symbol in archive_symbols}))
    archive_set = set(archives)
    current = exchange_info.current_usdt_perpetual_symbols
    current_set = set(current)
    with_archive = tuple(sorted(current_set.intersection(archive_set)))
    without_archive = tuple(sorted(current_set.difference(archive_set)))
    archive_only = tuple(sorted(archive_set.difference(current_set)))
    coverage_hash = _coverage_hash(
        exchange_info_hash=exchange_info.raw_sha256,
        archive_listing_hash=archive_listing_hash,
        raw_archive_symbol_count=len(archives),
        current_with_archive=with_archive,
        current_without_archive=without_archive,
        archive_only=archive_only,
    )
    return RegistryCoverageReport(
        exchange_info_hash=exchange_info.raw_sha256,
        archive_listing_hash=archive_listing_hash,
        raw_archive_symbol_count=len(archives),
        current_usdt_perpetual_count=len(current),
        current_with_archive=with_archive,
        current_without_archive=without_archive,
        archive_only=archive_only,
        coverage_hash=coverage_hash,
    )


def _atomic_write(destination: Path, content: bytes) -> Path:
    if destination.exists():
        if destination.read_bytes() != content:
            raise ExchangeInfoError(f"existing content-addressed file changed: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _model_bytes(model: BaseModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def write_exchange_info_snapshot(
    snapshot: ExchangeInfoSnapshot,
    data_dir: Path,
) -> tuple[Path, Path]:
    """Persist raw evidence separately from the compact parsed manifest."""

    raw_path = data_dir / "raw" / "binance" / "exchange_info" / f"{snapshot.raw_sha256}.json"
    manifest_path = (
        data_dir / "manifests" / "exchange_info" / f"{snapshot.raw_sha256}.json"
    )
    return (
        _atomic_write(raw_path, snapshot.raw_payload),
        _atomic_write(manifest_path, _model_bytes(snapshot)),
    )


def write_registry_coverage_report(
    report: RegistryCoverageReport,
    data_dir: Path,
) -> Path:
    """Persist one content-addressed coverage report."""

    destination = (
        data_dir / "manifests" / "registry_coverage" / f"{report.coverage_hash}.json"
    )
    return _atomic_write(destination, _model_bytes(report))
