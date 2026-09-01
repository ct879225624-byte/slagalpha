"""DEV run provenance and immutable result bundles; no execution or promotion authority."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from slagalpha.research.splits import DatasetRole


class RunManifestError(ValueError):
    """A run's claimed provenance or saved contents cannot be trusted."""


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize JSON-compatible results without non-finite numbers or key-order noise."""

    if not isinstance(payload, dict):
        raise RunManifestError("run payload must be a JSON object")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha256(value: str) -> str:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("input reference must be lowercase SHA-256")
    return value


class DevRunInputs(BaseModel):
    """Declared inputs from the frozen data contract; not proof that a run is authorized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    run_kind: Literal["DEV_RESEARCH", "NON_REPRODUCIBLE_DEV_RUN"]
    code_commit: str | None
    dirty_worktree: bool = Field(strict=True)
    strategy_version: str
    parameter_version: str
    schema_versions: tuple[str, ...] = Field(min_length=1)
    contract_registry_version: str
    exclusion_ledger_version: str
    universe_versions: tuple[str, ...] = Field(min_length=1)
    candle_dataset_hashes: tuple[str, ...] = Field(min_length=1)
    archive_manifest_hash: str
    cost_model_version: str
    random_seed: int | None = Field(default=None, strict=True, ge=0)
    command_arguments: tuple[str, ...] = Field(min_length=1)
    environment_lock_hash: str
    split_hash: str
    sensitivity_plan_hash: str

    @field_validator("code_commit")
    @classmethod
    def validate_commit(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("code_commit must be a full Git object ID or null")
        return value

    @field_validator(
        "strategy_version", "parameter_version", "contract_registry_version",
        "exclusion_ledger_version", "cost_model_version",
    )
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("version references must be non-empty without surrounding whitespace")
        return value

    @field_validator(
        "archive_manifest_hash", "environment_lock_hash", "split_hash", "sensitivity_plan_hash"
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("schema_versions", "universe_versions", "candle_dataset_hashes")
    @classmethod
    def validate_order(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("input reference sets must be unique and canonical")
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("input references must not be blank or padded")
        return values

    @field_validator("universe_versions", "candle_dataset_hashes")
    @classmethod
    def validate_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_sha256(value) for value in values)

    @field_validator("command_arguments")
    @classmethod
    def validate_arguments(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("command arguments must not be blank")
        return values

    @model_validator(mode="after")
    def validate_reproducibility(self) -> Self:
        if self.run_kind == "DEV_RESEARCH" and (self.code_commit is None or self.dirty_worktree):
            raise ValueError("formal DEV provenance requires a Git commit and a clean worktree")
        return self


class DevRunManifest(DevRunInputs):
    """A completed DEV result record, not a locked-test record or a strategy verdict."""

    schema_version: Literal["dev-run-manifest/0.1.0"] = "dev-run-manifest/0.1.0"
    started_at: datetime
    finished_at: datetime
    result_content_hash: str
    run_id: str

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("run timestamps must use UTC")
        if value.microsecond % 1000:
            raise ValueError("run timestamps must use exact millisecond precision")
        return value

    @field_serializer("started_at", "finished_at")
    def serialize_time(self, value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @field_validator("result_content_hash", "run_id")
    @classmethod
    def validate_result_hash(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        payload = self.model_dump(mode="json", exclude={"run_id"})
        if self.run_id != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("run manifest content hash mismatch")
        return self


def build_dev_run_manifest(
    inputs: DevRunInputs,
    *,
    started_at: datetime,
    finished_at: datetime,
    result: dict[str, Any],
) -> DevRunManifest:
    """Bind a completed result; this does not run P7 or relax any research input gate."""

    inputs = DevRunInputs.model_validate(inputs.model_dump(mode="json"))
    # Validate times before formatting so sub-millisecond or non-UTC values cannot disappear.
    for timestamp in (started_at, finished_at):
        DevRunManifest.validate_time(timestamp)
    payload = {
        **inputs.model_dump(mode="json"),
        "schema_version": "dev-run-manifest/0.1.0",
        "started_at": started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "finished_at": finished_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "result_content_hash": hashlib.sha256(canonical_json_bytes(result)).hexdigest(),
    }
    return DevRunManifest.model_validate({
        **payload, "run_id": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    })


def _publish_immutable(destination: Path, content: bytes) -> None:
    """Publish complete bytes without replacing a concurrently created destination."""

    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != content:
                raise RunManifestError("existing run artifact changed") from None
    finally:
        temporary.unlink(missing_ok=True)


def write_dev_run(
    manifest: DevRunManifest, result: dict[str, Any], artifacts_dir: Path,
) -> Path:
    """Write result first and manifest last; never silently repair a completed run."""

    manifest = DevRunManifest.model_validate(manifest.model_dump(mode="json"))
    result_bytes = canonical_json_bytes(result)
    if hashlib.sha256(result_bytes).hexdigest() != manifest.result_content_hash:
        raise RunManifestError("run result hash does not match manifest")
    directory = artifacts_dir / "runs" / manifest.run_id
    if not directory.resolve().is_relative_to(artifacts_dir.resolve()):
        raise RunManifestError("run directory escapes artifacts root")
    result_path = directory / "result.json"
    manifest_path = directory / "manifest.json"
    manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
    if manifest_path.exists() and not result_path.exists():
        raise RunManifestError("completed run is missing result; refusing silent repair")
    for path, content in ((result_path, result_bytes), (manifest_path, manifest_bytes)):
        if path.is_symlink() or (path.exists() and path.read_bytes() != content):
            raise RunManifestError("existing run artifact changed")
    directory.mkdir(parents=True, exist_ok=True)
    _publish_immutable(result_path, result_bytes)
    _publish_immutable(manifest_path, manifest_bytes)
    return directory


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunManifestError("duplicate JSON key in run artifact")
        result[key] = value
    return result


def _load_object(content: bytes) -> dict[str, Any]:
    parsed: Any = json.loads(content, object_pairs_hook=_unique_object)
    if not isinstance(parsed, dict):
        raise RunManifestError("run artifact must be a JSON object")
    if canonical_json_bytes(parsed) != content:
        raise RunManifestError("run artifact is not canonical JSON")
    return parsed


def read_dev_run(directory: Path) -> tuple[DevRunManifest, dict[str, Any]]:
    """Revalidate the manifest, directory identity and the saved result before reuse."""

    manifest_path = directory / "manifest.json"
    result_path = directory / "result.json"
    if manifest_path.is_symlink() or result_path.is_symlink():
        raise RunManifestError("run artifacts must not be symlinks")
    manifest = DevRunManifest.model_validate(_load_object(manifest_path.read_bytes()))
    if directory.name != manifest.run_id:
        raise RunManifestError("run directory does not match manifest run_id")
    result_bytes = result_path.read_bytes()
    if hashlib.sha256(result_bytes).hexdigest() != manifest.result_content_hash:
        raise RunManifestError("saved run result hash does not match manifest")
    return manifest, _load_object(result_bytes)
