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
PLAN_HASH = "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9"
PARAMETER_HASH = "81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b"

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
     "74e9a89210257478ba1b7ef89fd67ff3d6d12d87f6c44b6a4113369f30aab4f3.json",
     "a27efd78300f23631e53e5a3ebf934d1497039960f4e0d22b7c32c4bb0659202"),
    (InputArtifactRole.SENSITIVITY_PLAN,
     "data/manifests/sensitivity_plan/"
     "688113f39f756bd0585bb44831393eb4a4b1e013a68b750fc8817031ef10fca9.json",
     "2be858312884abe88bf79083bb4a75ca3291d0aa98da23b29a536c8880d14d00"),
    (InputArtifactRole.PARAMETER_VERSION,
     "data/manifests/parameter_version/"
     "81a2c13d7c51473a3c753debd668718d98f7f34c04a3216a763c1c534891c21b.json",
     "45e11fcf4ff73acfca3f5d0be4a1b6c98c021db84672f97e3676de37aeaf7ecd"),
    (InputArtifactRole.CONTRACT_REGISTRY,
     "data/manifests/contract_registry/"
     "f2a9370598caed227566b0c0903b215cd491aea45588d56e1dc3aea1b4e45ea0.json",
     "063d9e90fd61aee96232eba1bd9a019e4a710373c0625086b728eb9ab9c91d5c"),
    (InputArtifactRole.EXCLUSION_LEDGER,
     "data/manifests/exclusion_ledger/"
     "6d975a9efba4902e35060f00845c0a7abb524790c9972705790eee5e7ead07f5.json",
     "6d975a9efba4902e35060f00845c0a7abb524790c9972705790eee5e7ead07f5"),
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
    (InputArtifactRole.NORMALIZATION_GAP_AUDIT,
     "data/manifests/normalization_gap_audit/"
     "9fdfe51b0a209451b2bae612f427ba33702b8225b71b03211372f0c986c1a7fc.json",
     "3f1a0c3669a9f527d93d1a9df46707ac1cd69573a81a213286a4376d8d9b1479"),
    (InputArtifactRole.DEPENDENCY_ARTIFACTS,
     "data/manifests/dependency_artifacts/"
     "060d25d955664a803bbdc39b1eecbd466cb57a4a9c5ee278c9c15cb4358bc567.json",
     "3740153d436cc461306f8027359d9c50de367e0c2c9f0b2c16e0c70f964a0bca"),
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
