"""Verify selected DEV input bytes and report every still-missing artifact category."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.research.execution_inputs import (
    InputArtifactRole,
    InputArtifactSelection,
    inspect_dev_execution_inputs,
    write_dev_execution_input_report,
)
from slagalpha.research.parameters import DevParameterVersion
from slagalpha.research.sensitivity import SensitivityPlan

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "c41e2771a8ca526e4c8ffa09a863b078e300fc7332b9090461e11e1c1f2b5ff9"
PARAMETER_HASH = "56d5d45f745656ee7730ed152c2474f8a72aee3251f8d4f3fce02492f7da6cda"

SELECTIONS = (
    (InputArtifactRole.STRATEGY_RULES, "docs/strategy-rules-v0.1.md",
     "8275b2243149835a287c4f1ebf40ae6328d81e64dabaa8c3dad8bffc19054b93"),
    (InputArtifactRole.ENVIRONMENT_LOCK, "requirements.lock",
     "8c3d1ef3887544516ac06fa3efe7f9bcfc2b81b1f56b1da267cafa6a24574ee7"),
    (InputArtifactRole.RESEARCH_SPLIT,
     "data/manifests/research_split/"
     "b262a24e59f69d7887e8bd5805eb9f11c4fcaef6611c8cd7480d5a2ca89027ca.json",
     "0cc4f8fc002b77fa4cf767d610ccff2a4734006204104a8bd439313888f45615"),
    (InputArtifactRole.RESEARCH_INPUT_AUDIT,
     "data/manifests/research_input_audit/"
     "d58f1cc2adcad91ebbc33bc3864e1f78388b2c9c8f6adb83f1b88f5b3d99e864.json",
     "fe9b9ccaddccf802f719f647c67058d0921dd64bfd51a1dfcd6b56c306f749a6"),
    (InputArtifactRole.SENSITIVITY_PLAN,
     "data/manifests/sensitivity_plan/"
     "c41e2771a8ca526e4c8ffa09a863b078e300fc7332b9090461e11e1c1f2b5ff9.json",
     "5d80b4bcddf6afc4bbe28a4587554b119c202bb443a55607c287f155b81f752a"),
    (InputArtifactRole.PARAMETER_VERSION,
     "data/manifests/parameter_version/"
     "56d5d45f745656ee7730ed152c2474f8a72aee3251f8d4f3fce02492f7da6cda.json",
     "48670c2c6f6ee819c073267d3c032646ae11e1f3e23553057947e9e634408d11"),
    (InputArtifactRole.CONTRACT_REGISTRY,
     "data/manifests/contract_registry/"
     "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0.json",
     "063d9e90fd61aee96232eba1bd9a019e4a710373c0625086b728eb9ab9c91d5c"),
    (InputArtifactRole.UNIVERSE,
     "data/manifests/universe_batch/"
     "1b995e73691ae8b034f429677781e727716fb2909537f1f7643f04ec5cbae520.json",
     "0c00ed5a0ba08135891e4271ac1bb3a75b884e37fe89546e0cef6631e142284f"),
    (InputArtifactRole.CANDLE_MULTI_TIMEFRAME,
     "data/manifests/normalization_batch/"
     "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2.json",
     "36ee01b9b3370ad2ce567fc1c602d60b07ef6158fde45ded557b46e9c740844b"),
    (InputArtifactRole.ARCHIVE_MANIFEST,
     "data/manifests/archive_batch/"
     "9daba9a0aa2e4a61e3bb568cecec4e2c84166758dc24849ab974f2c4545edc0d.json",
     "1a072a32566cc6f55e512484352e0467cd29b5e24ba28f8ac342b27d7d1b0bd1"),
)


def main() -> int:
    manifests = ROOT / "data" / "manifests"
    plan = SensitivityPlan.model_validate_json(
        (manifests / "sensitivity_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    parameter = DevParameterVersion.model_validate_json(
        (manifests / "parameter_version" / f"{PARAMETER_HASH}.json").read_bytes()
    )
    selections = tuple(
        InputArtifactSelection(role=role, relative_path=path, expected_sha256=digest)
        for role, path, digest in SELECTIONS
    )
    report = inspect_dev_execution_inputs(
        project_dir=ROOT, plan=plan, parameter=parameter, selections=selections
    )
    path = write_dev_execution_input_report(report, ROOT / "data")
    print(json.dumps({
        **report.model_dump(mode="json"), "report_path": str(path.relative_to(ROOT)),
    }, sort_keys=True))
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
