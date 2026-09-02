"""Hash and revalidate the wheel files for an exact local research environment."""

from __future__ import annotations

import hashlib
import re
import zipfile
from email.parser import BytesParser
from itertools import product
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.reporting.environment import parse_environment_lock
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes


class DependencyArtifactError(ValueError):
    """A wheel set is incomplete, unsafe, incompatible, or changed."""


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value or not path.parts or str(path) != value or path.is_absolute() or "\\" in value
        or any(part in (".", "..") or ":" in part for part in path.parts)
    ):
        raise ValueError("artifact path must be canonical and project-relative")
    return value


def _directory(root: Path, relative: str) -> Path:
    _relative(relative)
    candidate = root
    if root.is_symlink():
        raise DependencyArtifactError("artifact root cannot be a symlink")
    for part in PurePosixPath(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise DependencyArtifactError("artifact directory cannot contain a symlink")
    if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_dir():
        raise DependencyArtifactError("artifact directory is missing or outside project")
    return candidate


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


class WheelArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    tags: tuple[str, ...]

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if _relative(value) != PurePosixPath(value).name or not value.endswith(".whl"):
            raise ValueError("artifact filename must be a wheel basename")
        return value


class DependencyArtifactManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dependency-artifacts/0.1.0"] = "dependency-artifacts/0.1.0"
    environment_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_directory: str
    runtime: dict[str, str]
    wheels: tuple[WheelArtifact, ...]
    packages_installed: Literal[False] = False
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifact_directory")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        return _relative(value)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        names = tuple(item.name for item in self.wheels)
        if not names or names != tuple(sorted(set(names))):
            raise ValueError("wheel names must be nonempty, unique and canonical")
        if len({item.filename for item in self.wheels}) != len(self.wheels):
            raise ValueError("wheel filenames must be unique")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if hashlib.sha256(canonical_json_bytes(payload)).hexdigest() != self.report_hash:
            raise ValueError("dependency manifest content hash mismatch")
        return self


def _wheel(path: Path, runtime: dict[str, str]) -> WheelArtifact:
    if path.is_symlink() or not path.is_file():
        raise DependencyArtifactError("wheel is missing, non-file or symlink")
    # Deliberately support only the current local lock, not a speculative deployment matrix.
    if (runtime["python-implementation"], runtime["sys-platform"], runtime["machine"]) != (
        "CPython", "win32", "AMD64",
    ):
        raise DependencyArtifactError("unsupported wheel runtime; expected local Windows CPython")
    python_tag = "cp" + "".join(runtime["python-version"].split(".")[:2])
    parts = path.name.removesuffix(".whl").split("-")
    if len(parts) != 5 or not path.name.endswith(".whl"):
        raise DependencyArtifactError("unsupported wheel filename")
    distribution, version, interpreter, abi, platform = parts
    tags = tuple(sorted("-".join(tag) for tag in product(
        interpreter.split("."), abi.split("."), platform.split("."),
    )))
    supported = {"py3-none-any", "py3-none-win_amd64", f"{python_tag}-{python_tag}-win_amd64"}
    if not supported.intersection(tags):
        raise DependencyArtifactError("wheel is incompatible with locked runtime")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [name for name in archive.namelist()
                              if name.endswith(".dist-info/METADATA")]
            prefix = f"{distribution}-{version}.dist-info/"
            if metadata_names != [prefix + "METADATA"]:
                raise DependencyArtifactError("wheel metadata directory does not match filename")
            metadata_files = (prefix + "METADATA", prefix + "WHEEL")
            if any(archive.namelist().count(name) != 1 for name in metadata_files):
                raise DependencyArtifactError("wheel metadata must be unique")
            if any(archive.getinfo(name).file_size > 1024 * 1024 for name in metadata_files):
                raise DependencyArtifactError("wheel metadata is unexpectedly large")
            metadata, wheel = tuple(BytesParser().parsebytes(archive.read(name))
                                    for name in metadata_files)
            if (
                len(metadata.get_all("Name", [])) != 1
                or len(metadata.get_all("Version", [])) != 1
                or _name(str(metadata["Name"])) != _name(distribution)
                or str(metadata["Version"]) != version
                or tuple(sorted(wheel.get_all("Tag", []))) != tags
            ):
                raise DependencyArtifactError("wheel name/version/tags disagree with metadata")
    except (zipfile.BadZipFile, KeyError) as error:
        raise DependencyArtifactError("wheel archive cannot be read") from error
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise DependencyArtifactError("wheel changed during inspection")
    return WheelArtifact(
        name=_name(distribution), version=version, filename=path.name,
        sha256=digest.hexdigest(), size_bytes=after.st_size, tags=tags,
    )


def build_dependency_artifact_manifest(
    *, project_dir: Path, environment_lock: bytes, artifact_directory: str,
) -> DependencyArtifactManifest:
    runtime, pins = parse_environment_lock(environment_lock)
    directory = _directory(project_dir, artifact_directory)
    paths = tuple(sorted(directory.iterdir()))
    if any(not path.name.endswith(".whl") for path in paths):
        raise DependencyArtifactError("artifact directory must contain only wheels")
    wheels = tuple(sorted((_wheel(path, runtime) for path in paths), key=lambda item: item.name))
    if len(wheels) != len(pins) or {item.name: item.version for item in wheels} != pins:
        raise DependencyArtifactError("wheel set must exactly match environment dependency pins")
    payload: dict[str, Any] = {
        "schema_version": "dependency-artifacts/0.1.0",
        "environment_lock_sha256": hashlib.sha256(environment_lock).hexdigest(),
        "artifact_directory": artifact_directory, "runtime": runtime,
        "wheels": [item.model_dump(mode="json") for item in wheels], "packages_installed": False,
    }
    return DependencyArtifactManifest.model_validate({
        **payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def inspect_dependency_artifacts(
    *, project_dir: Path, environment_lock: bytes, manifest: DependencyArtifactManifest,
) -> None:
    manifest = DependencyArtifactManifest.model_validate(manifest.model_dump(mode="json"))
    observed = build_dependency_artifact_manifest(
        project_dir=project_dir, environment_lock=environment_lock,
        artifact_directory=manifest.artifact_directory,
    )
    if observed != manifest:
        raise DependencyArtifactError("dependency files no longer match frozen manifest")


def write_dependency_artifact_manifest(
    manifest: DependencyArtifactManifest, data_dir: Path,
) -> Path:
    manifest = DependencyArtifactManifest.model_validate(manifest.model_dump(mode="json"))
    destination = data_dir / "manifests" / "dependency_artifacts" / f"{manifest.report_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise DependencyArtifactError("dependency manifest path escapes data directory")
    content = canonical_json_bytes(manifest.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise DependencyArtifactError("existing dependency manifest changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination


def hashed_requirements(manifest: DependencyArtifactManifest) -> bytes:
    manifest = DependencyArtifactManifest.model_validate(manifest.model_dump(mode="json"))
    return ("\n".join(
        f"{wheel.name}=={wheel.version} --hash=sha256:{wheel.sha256}" for wheel in manifest.wheels
    ) + "\n").encode("utf-8")
