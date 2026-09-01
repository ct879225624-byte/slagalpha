"""Persistence checks for explicit exclusion-ledger input artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from slagalpha.data.universe import UniverseSelectionError, write_exclusion_ledger
from slagalpha.domain.universe import ExclusionLedger


def test_empty_ledger_round_trip_is_content_addressed_and_immutable(tmp_path: Path) -> None:
    ledger = ExclusionLedger(ledger_version="empty-ledger/0.1.0", entries=())
    path = write_exclusion_ledger(ledger, tmp_path)
    content = path.read_bytes()
    assert path.name == f"{hashlib.sha256(content).hexdigest()}.json"
    assert ExclusionLedger.model_validate_json(content) == ledger
    assert write_exclusion_ledger(ledger, tmp_path) == path
    path.write_bytes(b"changed")
    with pytest.raises(UniverseSelectionError, match="changed"):
        write_exclusion_ledger(ledger, tmp_path)


def test_different_ledger_version_cannot_alias_the_same_artifact(tmp_path: Path) -> None:
    first = ExclusionLedger(ledger_version="empty-ledger/0.1.0", entries=())
    second = ExclusionLedger(ledger_version="empty-ledger/0.2.0", entries=())
    assert write_exclusion_ledger(first, tmp_path) != write_exclusion_ledger(second, tmp_path)
