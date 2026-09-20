"""Execute the five owner-approved, exact Funding mark verification queries."""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from slagalpha.data.binance_usdm_transport import (
    BinanceTransportPlan,
    build_binance_transport_plan,
)
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.funding_mark_supplement import (
    FundingMarkSupplementArtifact,
    FundingMarkSupplementError,
    FundingMarkSupplementRun,
    FundingMarkUncertaintyReport,
    fetch_funding_mark_event,
    quantify_funding_mark_uncertainty,
    verify_funding_mark_supplement,
    write_funding_mark_supplement_artifact,
    write_funding_mark_supplement_run,
    write_funding_mark_uncertainty_report,
)
from slagalpha.research.market_data_requirements import (
    MarketDataRequirementPlan,
)
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    load_replay_market_data_artifact,
)

TARGETS = (
    ("ETHUSDT", "2023-08-20T08:00:00Z"),
    ("AVAXUSDT", "2023-08-24T08:00:00Z"),
    ("TRBUSDT", "2023-09-28T16:00:00Z"),
    ("TRXUSDT", "2023-10-01T00:00:00Z"),
    ("SOLUSDT", "2023-10-03T08:00:00Z"),
)


def _load_directory[ModelT: BaseModel](
    directory: Path, model: type[ModelT]
) -> tuple[ModelT, ...]:
    if not directory.is_dir():
        raise ValueError(f"artifact directory does not exist: {directory}")
    values = []
    for path in sorted(directory.glob("*.json")):
        value = model.model_validate_json(path.read_bytes())
        content_hash = getattr(value, "artifact_hash", None)
        if content_hash is not None and path.stem != content_hash:
            raise ValueError(f"artifact filename does not match its content hash: {path}")
        values.append(value)
    return tuple(values)


def _targets(
    report: DevSourceScanReport, market_data_dir: Path, project_dir: Path
) -> list[dict[str, Any]]:
    wanted = {
        (symbol, datetime.fromisoformat(stamp.replace("Z", "+00:00"))) for symbol, stamp in TARGETS
    }
    funding = _load_directory(
        market_data_dir / "manifests" / "replay_market_data", ReplayMarketDataArtifact
    )
    output: list[dict[str, Any]] = []
    for artifact in funding:
        if artifact.role != "FUNDING":
            continue
        accepted = next(
            (
                item.request
                for item in report.accepted_requests
                if item.request.request_hash == artifact.request_hash
            ),
            None,
        )
        if accepted is None:
            raise ValueError("Funding artifact is not bound to an accepted request")
        dataset = load_replay_market_data_artifact(
            project_dir=project_dir, request=accepted, artifact=artifact
        )
        for observation in dataset.observations:
            key = (accepted.request.armed.symbol, observation.settlement_time)
            if key in wanted:
                if observation.mark_price is not None:
                    raise ValueError(f"target already has nonempty original markPrice: {key}")
                output.append(
                    {
                        "request": accepted,
                        "artifact": artifact,
                        "symbol": key[0],
                        "funding_time": observation.settlement_time,
                        "funding_rate": observation.rate,
                    }
                )
    if {(item["symbol"], item["funding_time"]) for item in output} != wanted:
        raise ValueError("the five required target events were not found exactly once")
    return sorted(output, key=lambda item: (item["symbol"], item["funding_time"]))


def _cached_target(
    *, project_dir: Path, data_dir: Path, target: dict[str, Any]
) -> FundingMarkSupplementArtifact | None:
    matches = []
    for path in (data_dir / "manifests" / "funding_mark_supplement").glob("*.json"):
        artifact = FundingMarkSupplementArtifact.model_validate_json(path.read_bytes())
        if (
            artifact.request_hash == target["request"].request_hash
            and artifact.funding_artifact_hash == target["artifact"].artifact_hash
            and artifact.symbol == target["symbol"]
            and artifact.funding_time == target["funding_time"]
        ):
            matches.append(artifact)
    if len(matches) > 1:
        raise FundingMarkSupplementError("multiple cached supplements match one event")
    if matches:
        verify_funding_mark_supplement(project_dir=project_dir, artifact=matches[0])
        return matches[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Five-event Funding mark DEV supplement")
    parser.add_argument("report", type=Path)
    parser.add_argument("--project-dir", type=Path, default=Path("."))
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--transport-plan", type=Path, required=True)
    parser.add_argument("--approve-requirements-hash", required=True)
    parser.add_argument("--transport-plan-hash", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("artifacts/market_data"))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("拒绝：补证必须显式指定 --execute", file=sys.stderr)
        return 2
    try:
        project_dir = args.project_dir.resolve()
        report = DevSourceScanReport.model_validate_json(args.report.read_bytes())
        requirements = MarketDataRequirementPlan.model_validate_json(args.requirements.read_bytes())
        plan = BinanceTransportPlan.model_validate_json(args.transport_plan.read_bytes())
        if requirements.plan_hash != args.approve_requirements_hash:
            raise ValueError("requirements approval hash mismatch")
        if plan.transport_plan_hash != args.transport_plan_hash:
            raise ValueError("transport plan approval hash mismatch")
        expected_plan = build_binance_transport_plan(
            report=report, requirements=requirements, max_attempts=plan.tasks[0].max_attempts
        )
        if expected_plan.transport_plan_hash != plan.transport_plan_hash:
            raise ValueError("transport plan is not derived from the approved requirements")
        if args.requirements.name != f"{requirements.plan_hash}.requirements.json":
            raise ValueError("requirements filename must contain its authenticated hash")
        if args.transport_plan.stem != plan.transport_plan_hash:
            raise ValueError("transport plan filename must contain its authenticated hash")
        targets = _targets(report, args.data_dir, project_dir)
        artifacts = []
        uncertainties = []
        raw_bytes = 0
        with httpx.Client(timeout=30.0) as client:
            for target in targets:
                artifact = _cached_target(
                    project_dir=project_dir,
                    data_dir=args.data_dir.resolve(),
                    target=target,
                )
                if artifact is None:
                    artifact = fetch_funding_mark_event(
                        client=client,
                        project_dir=project_dir,
                        data_dir=args.data_dir.resolve(),
                        request_hash=target["request"].request_hash,
                        funding_artifact_hash=target["artifact"].artifact_hash,
                        symbol=target["symbol"],
                        funding_time=target["funding_time"],
                        funding_rate=target["funding_rate"],
                    )
                write_funding_mark_supplement_artifact(artifact, args.data_dir.resolve())
                artifacts.append(artifact)
                raw_bytes += (
                    (project_dir / Path(artifact.funding_history_relative_path)).stat().st_size
                )
                if artifact.mark_price_relative_path:
                    raw_bytes += (
                        (project_dir / Path(artifact.mark_price_relative_path)).stat().st_size
                    )
                uncertainty = quantify_funding_mark_uncertainty(
                    request=target["request"], artifact=artifact
                )
                uncertainties.append(uncertainty)
                print(canonical_json_bytes(uncertainty.model_dump(mode="json")).decode())
        uncertainty_payload = {
            "schema_version": "funding-mark-uncertainty/0.1.0",
            "supplement_artifact_hashes": sorted(item.artifact_hash for item in artifacts),
            "items": [item.model_dump(mode="json") for item in uncertainties],
        }
        uncertainty_report = FundingMarkUncertaintyReport.model_validate(
            {
                **uncertainty_payload,
                "report_hash": hashlib.sha256(
                    canonical_json_bytes(uncertainty_payload)
                ).hexdigest(),
            }
        )
        uncertainty_path = write_funding_mark_uncertainty_report(
            uncertainty_report, args.data_dir.resolve()
        )
        run_payload = {
            "schema_version": "funding-mark-supplement-run/0.1.0",
            "requirements_plan_hash": requirements.plan_hash,
            "transport_plan_hash": plan.transport_plan_hash,
            "source_report_hash": report.report_hash,
            "artifacts": sorted(item.artifact_hash for item in artifacts),
            "raw_body_bytes": raw_bytes,
        }
        run = FundingMarkSupplementRun.model_validate(
            {
                "artifact_hash": hashlib.sha256(canonical_json_bytes(run_payload)).hexdigest(),
                **run_payload,
            }
        )
        path = write_funding_mark_supplement_run(run, args.data_dir.resolve())
        print(f"uncertainty_report={uncertainty_path}")
        print(f"supplement_run={path}")
        return 0
    except (OSError, ValueError, ValidationError, FundingMarkSupplementError) as error:
        print(f"Funding mark supplement failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
