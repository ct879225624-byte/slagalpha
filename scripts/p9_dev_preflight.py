"""Read-only project preflight. Exit 1 on blockers; no strategy execution or Git mutation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from slagalpha.reporting.environment import EnvironmentLockError, inspect_environment_lock
from slagalpha.research.preflight import (
    build_dev_preflight_report,
    inspect_git_provenance,
    write_dev_preflight_report,
)
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchInputAuditReport, ResearchSplitManifest

ROOT = Path(__file__).resolve().parents[1]
SPLIT_HASH = "b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca"
AUDIT_HASH = "d58f1cc2adcad91ebbc33bc3864e1f78388b2c9c8f6adb83f1b88f5b3d99e864"
PLAN_HASH = "c41e2771a8ca526e4c8ffa09a863b078e300fc7332b9090461e11e1c1f2b5ff9"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment-lock", type=Path, default=ROOT / "requirements.lock",
        help="Verify this exact-version local environment lock without installing dependencies",
    )
    args = parser.parse_args()
    manifests = ROOT / "data" / "manifests"
    try:
        split = ResearchSplitManifest.model_validate_json(
            (manifests / "research_split" / f"{SPLIT_HASH}.json").read_bytes()
        )
        audit = ResearchInputAuditReport.model_validate_json(
            (manifests / "research_input_audit" / f"{AUDIT_HASH}.json").read_bytes()
        )
        plan = SensitivityPlan.model_validate_json(
            (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
        )
        if (split.split_hash, audit.report_hash, plan.plan_hash) != (
            SPLIT_HASH, AUDIT_HASH, PLAN_HASH
        ):
            raise ValueError("preflight artifact identity does not match its selected filename")
        lock_hash = None
        if args.environment_lock.exists():
            lock_hash = inspect_environment_lock(args.environment_lock)
        rules_path = ROOT / "docs" / "strategy-rules-v0.1.md"
        rules_hash = hashlib.sha256(rules_path.read_bytes()).hexdigest()
        report = build_dev_preflight_report(
            split=split, audit=audit, plan=plan, git=inspect_git_provenance(ROOT),
            strategy_rules_sha256=rules_hash, environment_lock_hash=lock_hash,
        )
        path = write_dev_preflight_report(report, ROOT / "data")
    except EnvironmentLockError as error:
        print(json.dumps({
            "status": "ENVIRONMENT_LOCK_MISMATCH", "reason": str(error),
            "research_authorized": False,
        }))
        return 2
    except (ValueError, OSError):
        print(json.dumps({"status": "PREFLIGHT_ERROR", "research_authorized": False}))
        return 2
    print(json.dumps({
        **report.model_dump(mode="json"), "report_path": str(path.relative_to(ROOT)),
    }, sort_keys=True))
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
