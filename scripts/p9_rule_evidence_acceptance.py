"""Negative real-data acceptance: current BTC rules must not pass as historical DEV rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
from pathlib import Path

from slagalpha.data.rule_evidence import (
    RuleEvidenceSubmission,
    audit_rule_evidence,
    write_rule_evidence_report,
)
from slagalpha.domain.universe import ContractRegistry
from slagalpha.research.splits import ResearchSplitManifest

ROOT = Path(__file__).resolve().parents[1]
SPLIT_HASH = "b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca"
RULE_REGISTRY_HASH = "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0"


def main() -> None:
    manifests = ROOT / "data" / "manifests"
    registry_path = manifests / "contract_registry" / f"{RULE_REGISTRY_HASH}.json"
    registry_bytes = registry_path.read_bytes()
    registry = ContractRegistry.model_validate_json(registry_bytes)
    split = ResearchSplitManifest.model_validate_json(
        (manifests / "research_split" / f"{SPLIT_HASH}.json").read_bytes()
    )
    original = next(entry for entry in registry.entries if entry.symbol == "BTCUSDT")
    snapshot_manifest = json.loads(
        (manifests / "exchange_info" / f"{original.source_snapshot_hash}.json").read_bytes()
    )
    observed = datetime.fromisoformat(snapshot_manifest["fetched_at"])
    # Deliberately invalid historical claim, in memory only. This is not new rule evidence.
    candidate = original.model_copy(update={
        "effective_from": datetime.combine(split.segments[0].start, time(), tzinfo=UTC),
        "effective_to": datetime.combine(split.segments[0].end_exclusive, time(), tzinfo=UTC),
    })
    submission = RuleEvidenceSubmission(
        candidate=candidate,
        snapshot_file=f"raw/binance/exchange_info/{original.source_snapshot_hash}.json",
        snapshot_observed_at=observed,
        collected_at=observed,
    )
    report = audit_rule_evidence(submission, evidence_root=ROOT / "data")
    if report.blockers != (
        "CONTINUITY_EVIDENCE_REQUIRED", "SNAPSHOT_OUTSIDE_CLAIMED_INTERVAL"
    ):
        raise RuntimeError("current-metadata negative acceptance did not produce expected blockers")
    if registry_path.read_bytes() != registry_bytes:
        raise RuntimeError("evidence intake unexpectedly modified the registry")
    path = write_rule_evidence_report(report, ROOT / "data")
    print(json.dumps({
        "acceptance": "CURRENT_RULES_REJECTED_FOR_DEV",
        "report_hash": report.report_hash,
        "blockers": report.blockers,
        "registry_modified": report.registry_modified,
        "research_authorized": report.research_authorized,
        "report_path": str(path.relative_to(ROOT)),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
