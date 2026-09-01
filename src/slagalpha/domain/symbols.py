"""Canonical validation for exchange-provided symbol and asset identifiers."""

from __future__ import annotations


def _normalize_exchange_identifier(
    value: str,
    *,
    kind: str,
    minimum_length: int,
    maximum_length: int,
) -> str:
    normalized = value.strip().upper()
    if not minimum_length <= len(normalized) <= maximum_length:
        raise ValueError(
            f"{kind} must contain {minimum_length}-{maximum_length} characters"
        )
    if any(not (character.isalnum() or character == "_") for character in normalized):
        raise ValueError(
            f"{kind} may contain only Unicode letters, numbers, or underscore"
        )
    return normalized


def normalize_symbol(value: str) -> str:
    """Normalize a safe exchange symbol without assuming ASCII-only listings."""

    return _normalize_exchange_identifier(
        value,
        kind="symbol",
        minimum_length=2,
        maximum_length=40,
    )


def normalize_asset(value: str) -> str:
    """Normalize a safe exchange asset identifier."""

    return _normalize_exchange_identifier(
        value,
        kind="asset",
        minimum_length=1,
        maximum_length=40,
    )
