"""Synthetic wheel tests; these archives are never installed or used as real dependencies."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.reporting.dependency_artifacts import (
    DependencyArtifactError,
    DependencyArtifactManifest,
    build_dependency_artifact_manifest,
    hashed_requirements,
    inspect_dependency_artifacts,
    write_dependency_artifact_manifest,
)

LOCK = b"""# python-version: 3.12.13
# python-implementation: CPython
# sys-platform: win32
# machine: AMD64
demo==1.0
"""


def _wheel(root: Path, *, tag: str = "py3-none-any", version: str = "1.0") -> Path:
    directory = root / "wheels"
    directory.mkdir(exist_ok=True)
    path = directory / f"demo-1.0-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("demo-1.0.dist-info/METADATA", f"Name: demo\nVersion: {version}\n")
        archive.writestr("demo-1.0.dist-info/WHEEL", f"Wheel-Version: 1.0\nTag: {tag}\n")
    return path


def _manifest(root: Path) -> DependencyArtifactManifest:
    return build_dependency_artifact_manifest(
        project_dir=root, environment_lock=LOCK, artifact_directory="wheels",
    )


def test_exact_wheel_set_round_trips_and_hashes_without_installing(tmp_path: Path) -> None:
    _wheel(tmp_path)
    manifest = _manifest(tmp_path)
    inspect_dependency_artifacts(project_dir=tmp_path, environment_lock=LOCK, manifest=manifest)
    path = write_dependency_artifact_manifest(manifest, tmp_path / "data")
    assert write_dependency_artifact_manifest(manifest, tmp_path / "data") == path
    assert DependencyArtifactManifest.model_validate_json(path.read_bytes()) == manifest
    assert hashed_requirements(manifest) == (
        f"demo==1.0 --hash=sha256:{manifest.wheels[0].sha256}\n".encode()
    )
    assert manifest.packages_installed is False


def test_changed_wheel_bytes_are_rejected_even_when_metadata_unchanged(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    manifest = _manifest(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("unrelated.txt", "tamper")
    with pytest.raises(DependencyArtifactError, match="no longer match"):
        inspect_dependency_artifacts(
            project_dir=tmp_path, environment_lock=LOCK, manifest=manifest,
        )


@pytest.mark.parametrize("tag", ["cp311-cp311-win_amd64", "py3-none-linux_x86_64"])
def test_incompatible_wheels_are_rejected(tmp_path: Path, tag: str) -> None:
    _wheel(tmp_path, tag=tag)
    with pytest.raises(DependencyArtifactError, match="incompatible"):
        _manifest(tmp_path)


def test_metadata_version_mismatch_is_rejected(tmp_path: Path) -> None:
    _wheel(tmp_path, version="2.0")
    with pytest.raises(DependencyArtifactError, match="disagree"):
        _manifest(tmp_path)


def test_missing_extra_and_nonwheel_files_fail_closed(tmp_path: Path) -> None:
    directory = tmp_path / "wheels"
    directory.mkdir()
    with pytest.raises(DependencyArtifactError, match="exactly match"):
        _manifest(tmp_path)
    _wheel(tmp_path)
    extra = directory / "extra.txt"
    extra.write_text("synthetic", encoding="utf-8")
    with pytest.raises(DependencyArtifactError, match="only wheels"):
        _manifest(tmp_path)


def test_lock_drift_and_manifest_tampering_are_rejected(tmp_path: Path) -> None:
    _wheel(tmp_path)
    manifest = _manifest(tmp_path)
    with pytest.raises(DependencyArtifactError, match="exactly match"):
        inspect_dependency_artifacts(
            project_dir=tmp_path, environment_lock=LOCK.replace(b"demo==1.0", b"demo==2.0"),
            manifest=manifest,
        )
    payload = manifest.model_dump(mode="json")
    payload["environment_lock_sha256"] = "a" * 64
    with pytest.raises(ValidationError, match="content hash"):
        DependencyArtifactManifest.model_validate(payload)


@pytest.mark.parametrize("relative", ["../wheels", "/wheels", "C:/wheels", "a\\b", "."])
def test_unsafe_artifact_directory_is_rejected(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        build_dependency_artifact_manifest(
            project_dir=tmp_path, environment_lock=LOCK, artifact_directory=relative,
        )
