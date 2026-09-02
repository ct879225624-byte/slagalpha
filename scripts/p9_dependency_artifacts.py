"""Freeze existing local wheels and write a hash-pinned offline verification input."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.reporting.dependency_artifacts import (
    build_dependency_artifact_manifest,
    hashed_requirements,
    inspect_dependency_artifacts,
    write_dependency_artifact_manifest,
)
from slagalpha.reporting.environment import inspect_environment_lock
from slagalpha.reporting.run_manifest import _publish_immutable

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    lock = ROOT / "requirements.lock"
    inspect_environment_lock(lock)
    manifest = build_dependency_artifact_manifest(
        project_dir=ROOT, environment_lock=lock.read_bytes(),
        artifact_directory="data/dependency-artifacts",
    )
    path = write_dependency_artifact_manifest(manifest, ROOT / "data")
    inspect_dependency_artifacts(
        project_dir=ROOT, environment_lock=lock.read_bytes(), manifest=manifest,
    )
    requirements_path = ROOT / "artifacts" / f"dependencies-{manifest.report_hash}.lock"
    requirements_path.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(requirements_path, hashed_requirements(manifest))
    print(json.dumps({
        "report_hash": manifest.report_hash, "package_count": len(manifest.wheels),
        "total_bytes": sum(item.size_bytes for item in manifest.wheels),
        "report_path": str(path.relative_to(ROOT)),
        "hashed_requirements_path": str(requirements_path.relative_to(ROOT)),
        "packages_installed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
