"""Run a recoverable P9 DEV replay batch from immutable prepared inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from slagalpha.backtest.costs import CostScenario
from slagalpha.research.batch_replay import (
    BatchReplayInputError,
    run_replay_batch,
)
from slagalpha.research.funding_mark_supplement import FundingMarkSupplementArtifact
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.replay_market_data import ReplayMarketDataArtifact
from slagalpha.research.replay_prep import ReplayReadyManifest
from slagalpha.research.sensitivity import SensitivityPlan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute candidate-bound P7 requests without strategy selection."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--parameter-dir", type=Path, required=True)
    parser.add_argument("--market-data-dir", type=Path, required=True)
    parser.add_argument("--funding-mark-supplement-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument(
        "--candidate-hash",
        action="append",
        help="Explicit execution scope only; omission runs every candidate-bound request.",
    )
    parser.add_argument(
        "--cost-scenario",
        action="append",
        choices=[item.value for item in CostScenario],
        help="Explicit execution scope only; omission runs ZERO, BASELINE, and STRESS.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = ReplayReadyManifest.model_validate_json(args.manifest.read_bytes())
        plan = SensitivityPlan.model_validate_json(args.plan.read_bytes())
        parameters = tuple(
            DevParameterVersion.model_validate_json(path.read_bytes())
            for path in sorted(args.parameter_dir.glob("*.json"))
        )
        market_data = tuple(
            ReplayMarketDataArtifact.model_validate_json(path.read_bytes())
            for path in sorted(args.market_data_dir.glob("*.json"))
        )
        supplements = (
            tuple(
                FundingMarkSupplementArtifact.model_validate_json(path.read_bytes())
                for path in sorted((args.funding_mark_supplement_dir or Path(".")).glob("*.json"))
            )
            if args.funding_mark_supplement_dir is not None
            else ()
        )
        output_dir = args.output_dir
        checkpoint = run_replay_batch(
            project_dir=args.project_dir,
            manifest=manifest,
            plan=plan,
            parameter_versions=parameters,
            market_data=market_data,
            funding_mark_supplements=supplements,
            results_dir=output_dir,
            checkpoint_path=args.checkpoint or output_dir / "checkpoint.json",
            candidate_hashes=(tuple(args.candidate_hash) if args.candidate_hash else None),
            cost_scenarios=(
                tuple(CostScenario(value) for value in args.cost_scenario)
                if args.cost_scenario
                else None
            ),
        )
    except (OSError, ValueError, ValidationError, BatchReplayInputError) as error:
        print(json.dumps({"status": "FAILED", "reason": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(checkpoint.model_dump(mode="json"), sort_keys=True))
    return 0 if checkpoint.status == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
