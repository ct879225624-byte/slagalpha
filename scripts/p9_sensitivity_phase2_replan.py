"""Run one explicit, ledger-bound P9 Phase 2 P6 replan candidate.

This CLI intentionally performs no scanner, market-data download, or replay.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from slagalpha.domain.universe import ContractRegistry
from slagalpha.research.replan_sensitivity import (
    FINAL_LEDGER_HASH,
    SUPPORTED_CANDIDATES,
    ReplanCandidateFailed,
    ReplanExecutorError,
    _load_parameter,
    _load_registry,
    _load_split,
    _load_universes,
    build_failed_artifact,
    execute_replan_candidate,
    load_final_ledger,
    write_replan_artifact,
)
from slagalpha.research.sensitivity import SensitivityPlan

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ledger-bound P9 Phase 2 P6 replan; no scan/download/replay."
    )
    parser.add_argument(
        "--candidate-hash", required=True, choices=tuple(SUPPORTED_CANDIDATES)
    )
    parser.add_argument(
        "--ledger", type=Path,
        default=ROOT / "data" / "manifests" / "confirmed_trigger_ledger"
        / f"{FINAL_LEDGER_HASH}.json",
    )
    parser.add_argument(
        "--plan", type=Path,
        default=ROOT / "data" / "manifests" / "sensitivity_plan" / f"{PLAN_HASH}.json",
    )
    parser.add_argument("--parameter", type=Path)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "artifacts" / "sens_p2_replan"
    )
    args = parser.parse_args(argv)

    plan = SensitivityPlan.model_validate_json(args.plan.read_bytes())
    if plan.plan_hash != PLAN_HASH:
        raise ReplanExecutorError("Phase 2 requires the active frozen sensitivity plan")
    ledger = load_final_ledger(args.ledger)
    expected_parameter_hash = SUPPORTED_CANDIDATES[args.candidate_hash]
    parameter_path = args.parameter or (
        ROOT / "data" / "manifests" / "parameter_version"
        / f"{expected_parameter_hash}.json"
    )
    parameter = _load_parameter(
        parameter_path,
        plan=plan,
        candidate_hash=args.candidate_hash,
        expected_content_hash=expected_parameter_hash,
    )
    split = _load_split(ROOT / "data", plan)
    registry: ContractRegistry = _load_registry(ROOT / "data", ledger.registry_content_hash)
    universes = _load_universes(ROOT / "data")
    try:
        artifact = execute_replan_candidate(
            candidate_hash=args.candidate_hash,
            ledger=ledger,
            plan=plan,
            parameter=parameter,
            split=split,
            registry=registry,
            universes=universes,
        )
    except ReplanCandidateFailed as error:
        artifact = build_failed_artifact(ledger=ledger, parameter=parameter, error=error)
        write_replan_artifact(artifact, args.output_dir)
        print(artifact.model_dump_json(indent=2))
        return 1
    path = write_replan_artifact(artifact, args.output_dir)
    print(artifact.model_dump_json(indent=2))
    print(f"artifact_path={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
