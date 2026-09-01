"""Deterministic daily historical Universe selection over frozen evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pandas as pd

from slagalpha.domain.universe import (
    ContractIdentityLifecycleEntry,
    ContractIdentityRegistry,
    ContractRegistry,
    ContractRegistryEntry,
    ExclusionLedger,
    UniverseBlockedCandidate,
    UniverseBlockReason,
    UniverseMember,
    UniverseSnapshot,
)

UNIVERSE_SELECTION_VERSION = "daily-top30/0.1.0"
REQUIRED_INTERVALS = ("15m", "1h", "4h", "1d")
FIXED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
TARGET_MEMBER_COUNT = 30
MINIMUM_CONTRACT_AGE = timedelta(days=90)
RANKING_CANDLE_COUNT = 96
RANKING_INTERVAL = timedelta(minutes=15)


class UniverseSelectionError(ValueError):
    """Raised when global selection input cannot produce an auditable snapshot."""


@dataclass(frozen=True)
class _EligibleCandidate:
    symbol: str
    quote_volume: Decimal
    eligibility_refs: tuple[str, ...]
    candle_rows: tuple[dict[str, str], ...]


def _normalize_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise UniverseSelectionError("symbol mapping key must not be empty")
    return normalized


def _normalize_mapping[T](values: Mapping[str, T], name: str) -> dict[str, T]:
    normalized: dict[str, T] = {}
    for raw_symbol, value in values.items():
        symbol = _normalize_symbol(raw_symbol)
        if symbol in normalized:
            raise UniverseSelectionError(f"{name} contains duplicate normalized symbol {symbol}")
        normalized[symbol] = value
    return normalized


def _require_selection_time(selected_at: datetime) -> datetime:
    if selected_at.tzinfo is None or selected_at.utcoffset() != timedelta(0):
        raise UniverseSelectionError("selected_at must use UTC")
    midnight = selected_at.replace(hour=0, minute=0, second=0, microsecond=0)
    if selected_at != midnight + timedelta(minutes=5):
        raise UniverseSelectionError("selected_at must be exactly 00:05 UTC")
    return midnight


def _active_registry_entry(
    registry: ContractRegistry | ContractIdentityRegistry,
    symbol: str,
    selected_at: datetime,
) -> ContractRegistryEntry | ContractIdentityLifecycleEntry | None:
    matches = tuple(
        entry
        for entry in registry.entries
        if entry.symbol == symbol
        and entry.effective_from <= selected_at
        and (entry.effective_to is None or selected_at < entry.effective_to)
    )
    if len(matches) > 1:
        raise UniverseSelectionError(
            f"registry has multiple active entries for {symbol} at selection time"
        )
    return matches[0] if matches else None


def _active_exclusion_refs(
    ledger: ExclusionLedger,
    symbol: str,
    selected_at: datetime,
) -> tuple[str, ...]:
    refs = {
        f"exclusion:{entry.category.value}:{entry.evidence_ref}"
        for entry in ledger.entries
        if entry.symbol == symbol
        and entry.effective_from <= selected_at
        and (entry.effective_to is None or selected_at < entry.effective_to)
    }
    return tuple(sorted(refs))


def _decimal(value: object) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise UniverseSelectionError("quote_volume contains an invalid decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise UniverseSelectionError("quote_volume must be finite and non-negative")
    return parsed


def _utc_datetime(value: object, field: str) -> datetime:
    if isinstance(value, pd.Timestamp):
        converted = cast(datetime, value.to_pydatetime())
    elif isinstance(value, datetime):
        converted = value
    else:
        raise UniverseSelectionError(f"{field} must contain datetime values")
    if converted.tzinfo is None or converted.utcoffset() != timedelta(0):
        raise UniverseSelectionError(f"{field} must use UTC")
    return converted


def _window_volume(
    symbol: str,
    frame: pd.DataFrame,
    window_start: datetime,
    window_end: datetime,
) -> tuple[Decimal, tuple[dict[str, str], ...]] | UniverseBlockReason:
    required_columns = {
        "open_time",
        "close_time_exclusive",
        "quote_volume",
        "is_closed",
    }
    if not required_columns.issubset(frame.columns):
        return UniverseBlockReason.DATA_INCOMPLETE

    window_rows: list[tuple[datetime, datetime, Decimal, bool, str]] = []
    try:
        for row in frame.itertuples(index=False):
            values = row._asdict()
            open_time = _utc_datetime(values["open_time"], "open_time")
            if open_time < window_start or open_time >= window_end:
                continue
            close_time = _utc_datetime(
                values["close_time_exclusive"],
                "close_time_exclusive",
            )
            row_symbol = str(values.get("symbol", symbol)).strip().upper()
            row_interval = str(values.get("interval", "15m")).strip()
            if row_symbol != symbol or row_interval != "15m":
                return UniverseBlockReason.CANDLE_INVALID
            closed_value = values["is_closed"]
            if not isinstance(closed_value, bool) or not closed_value:
                return UniverseBlockReason.CANDLE_INVALID
            source_hash = str(values.get("source_file_hash", "")).strip().lower()
            window_rows.append(
                (
                    open_time,
                    close_time,
                    _decimal(values["quote_volume"]),
                    closed_value,
                    source_hash,
                )
            )
    except UniverseSelectionError:
        return UniverseBlockReason.CANDLE_INVALID

    by_open: dict[datetime, tuple[datetime, datetime, Decimal, bool, str]] = {}
    for row in window_rows:
        previous = by_open.get(row[0])
        if previous is not None and previous != row:
            return UniverseBlockReason.DUPLICATE_CONFLICT
        by_open[row[0]] = row

    expected_opens = tuple(
        window_start + index * RANKING_INTERVAL
        for index in range(RANKING_CANDLE_COUNT)
    )
    if tuple(sorted(by_open)) != expected_opens:
        return UniverseBlockReason.CANDLE_GAP

    ordered = tuple(by_open[open_time] for open_time in expected_opens)
    for open_time, close_time, _, _, _ in ordered:
        if close_time != open_time + RANKING_INTERVAL or close_time > window_end:
            return UniverseBlockReason.CANDLE_INVALID

    canonical_rows = tuple(
        {
            "symbol": symbol,
            "open_time": open_time.isoformat(),
            "close_time_exclusive": close_time.isoformat(),
            "quote_volume": str(quote_volume),
            "source_file_hash": source_hash,
        }
        for open_time, close_time, quote_volume, _, source_hash in ordered
    )
    total = sum((row[2] for row in ordered), start=Decimal(0))
    return total, canonical_rows


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_universe_snapshot(snapshot: UniverseSnapshot, data_dir: Path) -> Path:
    """Persist one immutable, content-addressed Universe snapshot atomically."""

    destination = (
        data_dir
        / "manifests"
        / "universe_snapshot"
        / f"{snapshot.universe_version}.json"
    )
    content = (
        json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise UniverseSelectionError(
                f"existing content-addressed snapshot changed: {destination}"
            )
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_exclusion_ledger(ledger: ExclusionLedger, data_dir: Path) -> Path:
    """Persist the exact reviewed ledger bytes without changing its semantic version."""

    ledger = ExclusionLedger.model_validate(ledger.model_dump(mode="json"))
    content = (
        json.dumps(
            ledger.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    content_hash = hashlib.sha256(content).hexdigest()
    destination = (
        data_dir / "manifests" / "exclusion_ledger" / f"{content_hash}.json"
    )
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != content:
            raise UniverseSelectionError(
                f"existing content-addressed exclusion ledger changed: {destination}"
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _blocked(
    symbol: str,
    reason_codes: tuple[UniverseBlockReason, ...],
    evidence_refs: tuple[str, ...],
) -> UniverseBlockedCandidate:
    return UniverseBlockedCandidate(
        symbol=symbol,
        reason_codes=tuple(sorted(set(reason_codes), key=lambda reason: reason.value)),
        evidence_refs=tuple(sorted(set(evidence_refs))),
    )


def build_daily_universe(
    *,
    selected_at: datetime,
    registry: ContractRegistry | ContractIdentityRegistry,
    exclusion_ledger: ExclusionLedger,
    fifteen_minute_candles: Mapping[str, pd.DataFrame],
    interval_evidence: Mapping[str, Mapping[str, str]],
    blocking_reasons: Mapping[str, tuple[UniverseBlockReason, ...]] | None = None,
) -> UniverseSnapshot:
    """Build one Top-30 snapshot from information available at 00:05 UTC."""

    window_end = _require_selection_time(selected_at)
    window_start = window_end - timedelta(days=1)
    frames = _normalize_mapping(fifteen_minute_candles, "fifteen_minute_candles")
    evidence = _normalize_mapping(interval_evidence, "interval_evidence")
    external_blocks = _normalize_mapping(blocking_reasons or {}, "blocking_reasons")

    symbols = sorted(
        {
            *(entry.symbol for entry in registry.entries),
            *frames,
            *evidence,
            *external_blocks,
        }
    )
    eligible: list[_EligibleCandidate] = []
    blocked: list[UniverseBlockedCandidate] = []

    for symbol in symbols:
        if symbol in external_blocks and external_blocks[symbol]:
            blocked.append(
                _blocked(
                    symbol,
                    external_blocks[symbol],
                    tuple(f"input-block:{reason.value}" for reason in external_blocks[symbol]),
                )
            )
            continue

        entry = _active_registry_entry(registry, symbol, selected_at)
        registry_ref = f"registry:{registry.registry_version}"
        if entry is None:
            blocked.append(
                _blocked(
                    symbol,
                    (UniverseBlockReason.SYMBOL_INELIGIBLE,),
                    (registry_ref,),
                )
            )
            continue
        if not entry.eligible_for_locked_research:
            blocked.append(
                _blocked(
                    symbol,
                    (UniverseBlockReason.CONTRACT_UNVERIFIED,),
                    (registry_ref, entry.source_ref),
                )
            )
            continue
        mature_at = selected_at - MINIMUM_CONTRACT_AGE
        if (
            entry.status != "TRADING"
            or entry.onboard_date is None
            or entry.onboard_date > mature_at
            or entry.derived_first_candle_at > mature_at
        ):
            blocked.append(
                _blocked(
                    symbol,
                    (UniverseBlockReason.SYMBOL_INELIGIBLE,),
                    (registry_ref, entry.source_ref),
                )
            )
            continue

        exclusion_refs = _active_exclusion_refs(exclusion_ledger, symbol, selected_at)
        if exclusion_refs:
            blocked.append(
                _blocked(symbol, (UniverseBlockReason.EXCLUDED,), exclusion_refs)
            )
            continue

        symbol_evidence = evidence.get(symbol, {})
        missing_intervals = tuple(
            interval
            for interval in REQUIRED_INTERVALS
            if not str(symbol_evidence.get(interval, "")).strip()
        )
        if missing_intervals:
            blocked.append(
                _blocked(
                    symbol,
                    (UniverseBlockReason.DATA_INCOMPLETE,),
                    tuple(f"missing-interval:{interval}" for interval in missing_intervals),
                )
            )
            continue

        frame = frames.get(symbol)
        if frame is None:
            blocked.append(
                _blocked(
                    symbol,
                    (UniverseBlockReason.DATA_INCOMPLETE,),
                    ("missing-ranking-candles:15m",),
                )
            )
            continue
        window_result = _window_volume(symbol, frame, window_start, window_end)
        if isinstance(window_result, UniverseBlockReason):
            blocked.append(
                _blocked(
                    symbol,
                    (window_result,),
                    (f"ranking-window:{window_start.isoformat()}:{window_end.isoformat()}",),
                )
            )
            continue

        quote_volume, candle_rows = window_result
        eligibility_refs = (
            f"contract:{entry.source_ref}",
            *(
                f"interval:{interval}:{symbol_evidence[interval]}"
                for interval in REQUIRED_INTERVALS
            ),
        )
        eligible.append(
            _EligibleCandidate(
                symbol=symbol,
                quote_volume=quote_volume,
                eligibility_refs=eligibility_refs,
                candle_rows=candle_rows,
            )
        )

    ranked = sorted(eligible, key=lambda candidate: (-candidate.quote_volume, candidate.symbol))
    selected = list(ranked[:TARGET_MEMBER_COUNT])
    selected_symbols = {candidate.symbol for candidate in selected}
    eligible_by_symbol = {candidate.symbol: candidate for candidate in eligible}
    forced_symbols: set[str] = set()

    for fixed_symbol in FIXED_SYMBOLS:
        candidate = eligible_by_symbol.get(fixed_symbol)
        if candidate is None or fixed_symbol in selected_symbols:
            continue
        if len(selected) >= TARGET_MEMBER_COUNT:
            replacement_index = next(
                index
                for index in range(len(selected) - 1, -1, -1)
                if selected[index].symbol not in FIXED_SYMBOLS
            )
            removed = selected.pop(replacement_index)
            selected_symbols.remove(removed.symbol)
        selected.append(candidate)
        selected_symbols.add(fixed_symbol)
        forced_symbols.add(fixed_symbol)

    selected.sort(key=lambda candidate: (-candidate.quote_volume, candidate.symbol))
    members = tuple(
        UniverseMember(
            symbol=candidate.symbol,
            rank=index,
            rolling_quote_volume_24h=candidate.quote_volume,
            forced=candidate.symbol in forced_symbols,
            eligibility_refs=candidate.eligibility_refs,
        )
        for index, candidate in enumerate(selected, start=1)
    )
    blocked_candidates = tuple(sorted(blocked, key=lambda candidate: candidate.symbol))

    candle_dataset_hash = _canonical_sha256(
        [
            {
                "symbol": candidate.symbol,
                "rows": candidate.candle_rows,
            }
            for candidate in sorted(eligible, key=lambda candidate: candidate.symbol)
        ]
    )
    version_payload = {
        "selection_algorithm_version": UNIVERSE_SELECTION_VERSION,
        "selected_at": selected_at.isoformat(),
        "effective_from": (window_end + timedelta(minutes=15)).isoformat(),
        "effective_to": (window_end + timedelta(days=1, minutes=15)).isoformat(),
        "ranking_window_start": window_start.isoformat(),
        "ranking_window_end": window_end.isoformat(),
        "contract_registry_version": registry.registry_version,
        "exclusion_ledger_version": exclusion_ledger.ledger_version,
        "candle_dataset_hash": candle_dataset_hash,
        "members": [member.model_dump(mode="json") for member in members],
        "blocked_candidates": [
            candidate.model_dump(mode="json") for candidate in blocked_candidates
        ],
    }
    universe_version = _canonical_sha256(version_payload)

    return UniverseSnapshot(
        universe_version=universe_version,
        selection_algorithm_version=UNIVERSE_SELECTION_VERSION,
        selected_at=selected_at,
        effective_from=window_end + timedelta(minutes=15),
        effective_to=window_end + timedelta(days=1, minutes=15),
        ranking_window_start=window_start,
        ranking_window_end=window_end,
        member_count=len(members),
        members=members,
        blocked_candidates=blocked_candidates,
        contract_registry_version=registry.registry_version,
        exclusion_ledger_version=exclusion_ledger.ledger_version,
        candle_dataset_hash=candle_dataset_hash,
    )
