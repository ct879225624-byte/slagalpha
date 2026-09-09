"""Execute the explicitly approved local lifecycle boundary-month normalization."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from slagalpha.research.lifecycle_executor_contract import (
    LifecycleExecutorInterfaceContract,
)
from slagalpha.research.lifecycle_real_execution import (
    build_real_execution_authorization,
    execute_real_lifecycle_derivatives,
    write_real_execution_authorization,
)
from slagalpha.research.lifecycle_remediation import (
    LifecycleNormalizationRemediationPlan,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
CONTRACT_HASH = "e1033ff7559b3116e6825471911718dc16828ec6d99acc95d7e3d66d5a914fa3"
AUTHORIZED_AT = datetime(2026, 9, 9, 9, 13, 35, 349000, tzinfo=UTC)
APPROVAL: Literal["USER_APPROVED_REAL_LOCAL_LIFECYCLE_NORMALIZATION"] = (
    "USER_APPROVED_REAL_LOCAL_LIFECYCLE_NORMALIZATION"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute-authorized-real-lifecycle",
        action="store_true",
        help="required explicit acknowledgement for the approved local execution",
    )
    args = parser.parse_args()
    if not args.execute_authorized_real_lifecycle:
        parser.error("--execute-authorized-real-lifecycle is required")

    data_dir = ROOT / "data"
    plan = LifecycleNormalizationRemediationPlan.model_validate_json(
        (
            data_dir
            / "manifests"
            / "lifecycle_normalization_remediation_plan"
            / f"{PLAN_HASH}.json"
        ).read_bytes()
    )
    contract = LifecycleExecutorInterfaceContract.model_validate_json(
        (
            data_dir
            / "manifests"
            / "lifecycle_derivative_executor_contract"
            / f"{CONTRACT_HASH}.json"
        ).read_bytes()
    )
    authorization = build_real_execution_authorization(
        contract,
        plan,
        expected_contract_hash=CONTRACT_HASH,
        expected_plan_hash=PLAN_HASH,
        authorized_at=AUTHORIZED_AT,
        approval=APPROVAL,
    )
    authorization_path = write_real_execution_authorization(authorization, data_dir)
    receipt, receipt_path = execute_real_lifecycle_derivatives(
        authorization,
        contract,
        plan,
        raw_klines_dir=data_dir / "raw" / "binance" / "futures" / "um" / "monthly" / "klines",
        workspace_root=ROOT,
        normalized_at=AUTHORIZED_AT,
    )
    print(
        json.dumps(
            {
                "authorization_hash": authorization.authorization_hash,
                "authorization_path": str(authorization_path.relative_to(ROOT)),
                "receipt_hash": receipt.receipt_hash,
                "receipt_path": str(receipt_path.relative_to(ROOT)),
                "action_count": receipt.action_count,
                "materialized_action_count": receipt.materialized_action_count,
                "excluded_action_count": receipt.excluded_action_count,
                "source_row_count": receipt.source_row_count,
                "excluded_row_count": receipt.excluded_row_count,
                "retained_row_count": receipt.retained_row_count,
                "research_authorized": receipt.research_authorized,
                "strategy_executed": receipt.strategy_executed,
                "locked_test_consumed": receipt.locked_test_consumed,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
