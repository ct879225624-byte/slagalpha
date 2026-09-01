"""Audit a local unverified rule submission; exit 0 means ready for human review only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from slagalpha.data.rule_evidence import (
    RuleEvidenceSubmission,
    audit_rule_evidence,
    load_rule_evidence_submission,
    write_rule_evidence_report,
)

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission", type=Path, nargs="?")
    parser.add_argument("--evidence-root", type=Path, help="Default: submission parent directory")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--schema", action="store_true", help="Print the input JSON schema and exit"
    )
    args = parser.parse_args(argv)
    if args.schema:
        print(json.dumps(RuleEvidenceSubmission.model_json_schema(), indent=2, sort_keys=True))
        return 0
    if args.submission is None:
        parser.error("submission is required unless --schema is used")
    try:
        submission = load_rule_evidence_submission(args.submission.read_bytes())
        report = audit_rule_evidence(
            submission, evidence_root=args.evidence_root or args.submission.parent
        )
        path = write_rule_evidence_report(report, args.data_dir)
    except (ValueError, OSError):
        # Do not echo arbitrary evidence contents, paths or Pydantic input values.
        print(json.dumps({"status": "INVALID_SUBMISSION", "research_authorized": False}))
        return 2
    print(json.dumps({**report.model_dump(mode="json"), "report_path": str(path)}, sort_keys=True))
    return 1 if report.status == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
