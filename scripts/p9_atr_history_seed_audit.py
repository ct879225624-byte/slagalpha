"""Audit lifecycle warm-up evidence without running indicators or research."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from slagalpha.research.atr_history_audit import (
    LifecycleWarmupInput,
    build_atr_history_seed_audit,
    write_atr_history_seed_audit,
)
from slagalpha.research.lifecycle_real_execution import LifecycleRealExecutionReceipt
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan
from slagalpha.research.lifecycle_replacement import LifecycleReplacementNormalizationResult

ROOT = Path(__file__).resolve().parents[1]
REPLACEMENT_HASH = "7565da18af89195250651ba2cf49f60ae4f8725ce9a83a2b010e540e5047911b"
PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
RECEIPT_HASH = "3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e"


def _verified_output_sha256(relative_path: str, expected_sha256: str) -> str:
    output_root = (ROOT / "data/normalized/lifecycle_scoped/v0.1.0").resolve()
    path = ROOT.joinpath(*Path(relative_path).parts)
    resolved = path.resolve()
    if (
        not resolved.is_relative_to(output_root)
        or path.is_symlink()
        or not path.is_file()
    ):
        raise ValueError("lifecycle derivative output is unavailable or escapes its namespace")
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != expected_sha256:
        raise ValueError("lifecycle derivative output hash changed")
    return observed


def main() -> int:
    manifests = ROOT / "data" / "manifests"
    replacement = LifecycleReplacementNormalizationResult.model_validate_json(
        (
            manifests
            / "lifecycle_replacement_normalization"
            / f"{REPLACEMENT_HASH}.json"
        ).read_bytes()
    )
    plan = LifecycleNormalizationRemediationPlan.model_validate_json(
        (manifests / "lifecycle_normalization_remediation_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    receipt = LifecycleRealExecutionReceipt.model_validate_json(
        (manifests / "lifecycle_real_execution" / f"{RECEIPT_HASH}.json").read_bytes()
    )
    outputs = {f"{item.symbol}/{item.interval}": item for item in receipt.outputs}
    inputs = tuple(
        LifecycleWarmupInput(
            identity=f"{action.symbol}/{action.interval}",
            lifecycle_start=action.identity_effective_from,
            verified_post_cutoff_row_count=outputs[
                f"{action.symbol}/{action.interval}"
            ].retained_row_count,
            complete_same_lifecycle_prefix=False,
            contiguous_same_lifecycle_prefix=True,
            source_reference=_verified_output_sha256(
                outputs[f"{action.symbol}/{action.interval}"].output_relative_path,
                outputs[f"{action.symbol}/{action.interval}"].output_sha256,
            ),
        )
        for action in plan.actions
    )
    report = build_atr_history_seed_audit(
        replacement=replacement,
        expected_replacement_hash=REPLACEMENT_HASH,
        inputs=inputs,
        unresolved_lifecycle_identities=replacement.remaining_failure_identities,
    )
    path = write_atr_history_seed_audit(report, ROOT / "data")
    print(
        json.dumps(
            {**report.model_dump(mode="json"), "report_path": str(path.relative_to(ROOT))},
            sort_keys=True,
        )
    )
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
