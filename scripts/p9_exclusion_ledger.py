"""Persist the explicit empty ledger already referenced by every P8 Universe snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from slagalpha.data.universe import write_exclusion_ledger
from slagalpha.domain.universe import ExclusionLedger

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ledger = ExclusionLedger(ledger_version="empty-ledger/0.1.0", entries=())
    path = write_exclusion_ledger(ledger, ROOT / "data")
    print(json.dumps({
        "ledger_version": ledger.ledger_version,
        "entry_count": len(ledger.entries),
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "artifact_path": str(path.relative_to(ROOT)),
        "rules_added": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
