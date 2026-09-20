"""Plan or explicitly execute accepted-request-scoped Binance USD-M downloads."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from pydantic import ValidationError

from slagalpha.data.binance_usdm_transport import (
    BinanceTransportError,
    build_binance_transport_plan,
    finalize_binance_transport,
    run_binance_transport,
    write_binance_transport_plan,
)
from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.market_data_requirements import MarketDataRequirementPlan


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan accepted-request-scoped Binance USD-M public data transport. "
            "Network access requires both --execute and the exact approved requirements hash."
        )
    )
    parser.add_argument("report", type=Path, help="Final COMPLETE source-scan report JSON")
    parser.add_argument("requirements", type=Path, help="Post-scan requirements JSON")
    parser.add_argument("--project-dir", type=Path, default=Path("."))
    parser.add_argument("--data-dir", type=Path, default=Path("artifacts/market_data"))
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-requirements-hash")
    args = parser.parse_args()
    try:
        project_dir = args.project_dir.resolve()
        data_dir = (project_dir / args.data_dir).resolve()
        report = DevSourceScanReport.model_validate_json(args.report.read_bytes())
        requirements = MarketDataRequirementPlan.model_validate_json(
            args.requirements.read_bytes()
        )
        if args.requirements.name != f"{requirements.plan_hash}.requirements.json":
            raise BinanceTransportError(
                "requirements filename must contain its authenticated plan hash"
            )
        plan = build_binance_transport_plan(
            report=report,
            requirements=requirements,
            max_attempts=args.max_attempts,
        )
        plan_path = write_binance_transport_plan(plan, data_dir)
        print(f"requirements_hash={requirements.plan_hash}")
        print(f"transport_plan_hash={plan.transport_plan_hash}")
        print(f"accepted_requests={len(plan.accepted_request_hashes)}")
        print(f"deduplicated_http_tasks={len(plan.tasks)}")
        print(f"plan={plan_path}")
        if not args.execute:
            print("PLAN_ONLY: no network request was made")
            return 0
        if args.approve_requirements_hash is None:
            raise BinanceTransportError(
                "--execute requires --approve-requirements-hash"
            )
        with httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=False) as client:
            checkpoint = run_binance_transport(
                project_dir=project_dir,
                data_dir=data_dir,
                plan=plan,
                approved_requirements_plan_hash=args.approve_requirements_hash,
                client=client,
            )
        outputs = finalize_binance_transport(
            project_dir=project_dir,
            data_dir=data_dir,
            report=report,
            requirements=requirements,
            plan=plan,
        )
    except (OSError, ValueError, ValidationError, httpx.HTTPError) as error:
        print(f"P9 market-data transport failed: {error}", file=sys.stderr)
        return 2
    print(f"checkpoint={checkpoint.checkpoint_hash}")
    print(f"market_data_artifacts={len(outputs.market_data)}")
    print(f"funding_schedule_artifacts={len(outputs.funding_schedules)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
