"""Audit the retained public archive observation; always fail closed on interval coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from slagalpha.data.historical_rule_sources import (
    ArchivedExchangeInfoSource,
    HistoricalRuleSourceError,
    audit_archived_exchange_info,
    write_historical_rule_source_audit,
)
from slagalpha.research.rule_gaps import DevRuleGapReport

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = (
    ROOT / "data" / "raw" / "binance" / "exchange_info" / "wayback-20231102093209.json"
)
DEFAULT_GAP_REPORT = (
    ROOT
    / "data"
    / "manifests"
    / "dev_rule_gaps"
    / "78b11fb3e35e22ae90108f8be2e512342483022aae938dd02a983b2c1664d808.json"
)
EXPECTED_RAW_SHA256 = "6a3b256bcc2a05d3542897bc45df57417dc33b51506231099186ba9a73f21572"


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--gap-report", type=Path, default=DEFAULT_GAP_REPORT)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args(argv)
    try:
        raw = args.snapshot.read_bytes()
        if hashlib.sha256(raw).hexdigest() != EXPECTED_RAW_SHA256:
            raise HistoricalRuleSourceError("retained archive snapshot hash changed")
        gaps = DevRuleGapReport.model_validate_json(args.gap_report.read_bytes())
        maximum = max(target.blocked_member_day_count for target in gaps.targets)
        priority_symbols = tuple(
            target.symbol for target in gaps.targets if target.blocked_member_day_count == maximum
        )
        source = ArchivedExchangeInfoSource(
            replay_url=(
                "https://web.archive.org/web/20231102093209id_/"
                "https://fapi.binance.com/fapi/v1/exchangeInfo"
            ),
            archive_capture_at=_time("2023-11-02T09:32:09Z"),
            retrieved_at=_time("2026-09-09T14:13:08Z"),
            cdx_digest="7RTHSJVGSWZYNUP2ETU4ZTPTAMBXRFWM",
            raw_sha256=EXPECTED_RAW_SHA256,
        )
        report = audit_archived_exchange_info(
            raw,
            source=source,
            dev_rule_gap_hash=gaps.report_hash,
            target_symbols=tuple(target.symbol for target in gaps.targets),
            priority_symbols=priority_symbols,
        )
        path = write_historical_rule_source_audit(report, args.data_dir)
    except (HistoricalRuleSourceError, OSError, ValueError):
        print(json.dumps({"status": "INVALID_SOURCE", "research_authorized": False}))
        return 2
    print(json.dumps({**report.model_dump(mode="json"), "report_path": str(path)}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
