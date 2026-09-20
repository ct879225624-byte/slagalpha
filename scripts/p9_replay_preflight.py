"""Offline CLI: validate and build P9 DEV replay-ready inputs; never run replay."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError

from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.funding_mark_supplement import FundingMarkSupplementArtifact
from slagalpha.research.market_data_requirements import MarketDataRequirementPlan
from slagalpha.research.replay_market_data import ReplayMarketDataArtifact
from slagalpha.research.replay_prep import (
    FundingScheduleArtifact,
    build_resume_plan,
    load_replay_checkpoint,
    prepare_dev_replay,
    render_replay_readiness,
    write_replay_prep_artifacts,
    write_replay_resume_plan,
)


def _load_directory[ModelT: BaseModel](directory: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    if not directory.is_dir():
        raise ValueError(f"artifact directory does not exist: {directory}")
    loaded: list[ModelT] = []
    for path in sorted(directory.rglob("*.json")):
        item = model.model_validate_json(path.read_bytes())
        content_hash = getattr(item, "artifact_hash", None) or getattr(item, "schedule_hash", None)
        if content_hash is not None and path.stem != content_hash:
            raise ValueError(f"artifact filename does not match its content hash: {path}")
        loaded.append(item)
    return tuple(loaded)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline P9 DEV replay preflight; no network, download, replay, "
            "VALIDATION or LOCKED_TEST."
        )
    )
    parser.add_argument("report", type=Path, help="Final P9 DEV source-scan report JSON")
    parser.add_argument("--project-dir", type=Path, default=Path("."))
    parser.add_argument("--market-data-dir", type=Path, required=True)
    parser.add_argument("--funding-schedule-dir", type=Path, required=True)
    parser.add_argument("--funding-mark-supplement-dir", type=Path)
    parser.add_argument(
        "--requirements",
        type=Path,
        help="Authenticated post-scan requirements JSON (recommended for real artifacts)",
    )
    parser.add_argument(
        "--transport-plan",
        type=Path,
        help="Authenticated transport plan JSON (recommended for real artifacts)",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/replay_prep"))
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    try:
        report = DevSourceScanReport.model_validate_json(args.report.read_bytes())
        if len(args.report.stem) == 64 and args.report.stem != report.report_hash:
            raise ValueError("report filename must contain its authenticated report hash")
        market_data = _load_directory(args.market_data_dir, ReplayMarketDataArtifact)
        schedules = _load_directory(args.funding_schedule_dir, FundingScheduleArtifact)
        supplements = (
            _load_directory(args.funding_mark_supplement_dir, FundingMarkSupplementArtifact)
            if args.funding_mark_supplement_dir is not None
            else ()
        )
        if (args.requirements is None) != (args.transport_plan is None):
            raise ValueError("--requirements and --transport-plan must be supplied together")
        requirements = (
            MarketDataRequirementPlan.model_validate_json(args.requirements.read_bytes())
            if args.requirements is not None
            else None
        )
        transport_plan = None
        if args.transport_plan is not None:
            from slagalpha.data.binance_usdm_transport import BinanceTransportPlan

            transport_plan = BinanceTransportPlan.model_validate_json(
                args.transport_plan.read_bytes()
            )
        if requirements is not None and args.requirements.name != (
            f"{requirements.plan_hash}.requirements.json"
        ):
            raise ValueError("requirements filename must contain its authenticated plan hash")
        if transport_plan is not None and args.transport_plan.stem != (
            transport_plan.transport_plan_hash
        ):
            raise ValueError("transport-plan filename must contain its authenticated plan hash")
        result = prepare_dev_replay(
            project_dir=args.project_dir.resolve(),
            report=report,
            market_data=market_data,
            funding_schedules=schedules,
            funding_mark_supplements=supplements,
            requirements=requirements,
            transport_plan=transport_plan,
        )
        paths = write_replay_prep_artifacts(result, args.output_dir)
        resume = build_resume_plan(
            result.manifest,
            load_replay_checkpoint(args.checkpoint) if args.checkpoint is not None else None,
        )
        resume_path = write_replay_resume_plan(resume, args.output_dir)
        paths = (*paths, resume_path)
    except (OSError, ValueError, ValidationError) as error:
        print(f"ReplayPrep 输入无效：{error}", file=sys.stderr)
        return 2
    print(render_replay_readiness(result.summary), end="")
    if result.summary.requirements_plan_hash is not None:
        print(f"requirements_plan_hash={result.summary.requirements_plan_hash}")
    if result.summary.transport_plan_hash is not None:
        print(f"transport_plan_hash={result.summary.transport_plan_hash}")
    print(f"输出目录：{args.output_dir.resolve()}")
    print(f"输出文件：{len(paths)}")
    return 0 if result.summary.can_start_dev_replay else 1


if __name__ == "__main__":
    raise SystemExit(main())
