"""Offline cache and recovery primitives for a future public-data downloader."""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import sha256_file
from slagalpha.research.replay_inputs import Sha256


class DownloadCacheError(ValueError):
    """A staged response or cache entry failed integrity checks."""


class DownloadTask(BaseModel):
    """Canonical future request identity; it grants no network authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    endpoint: str
    parameters: tuple[tuple[str, str], ...]
    max_attempts: int = Field(default=5, ge=1, strict=True)
    cache_key: Sha256
    network_authorized: Literal[False] = False

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        if not self.provider.strip() or not self.endpoint.startswith("/"):
            raise ValueError("provider and absolute endpoint path are required")
        if self.parameters != tuple(sorted(set(self.parameters))):
            raise ValueError("download parameters must be unique and canonical")
        payload = f"{self.provider}\n{self.endpoint}\n{self.parameters!r}".encode()
        if self.cache_key != hashlib.sha256(payload).hexdigest():
            raise ValueError("download cache key mismatch")
        return self


def build_download_task(
    *, provider: str, endpoint: str, parameters: tuple[tuple[str, str], ...], max_attempts: int = 5
) -> DownloadTask:
    ordered = tuple(sorted(parameters))
    payload = f"{provider}\n{endpoint}\n{ordered!r}".encode()
    return DownloadTask(
        provider=provider,
        endpoint=endpoint,
        parameters=ordered,
        max_attempts=max_attempts,
        cache_key=hashlib.sha256(payload).hexdigest(),
    )


class DownloadCheckpoint(BaseModel):
    """Small mutable-state payload supporting bounded retries and validated resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_key: Sha256
    attempts_completed: int = Field(ge=0, strict=True)
    max_attempts: int = Field(ge=1, strict=True)
    bytes_received: int = Field(ge=0, strict=True)
    etag: str | None = None
    last_modified: str | None = None
    last_error: str | None = None
    retry_not_before: datetime | None = None
    state: Literal["PLANNED", "PARTIAL", "RETRY_WAIT", "EXHAUSTED"]

    @field_validator("retry_not_before")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("retry time must use UTC")
        return value

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        if self.attempts_completed > self.max_attempts:
            raise ValueError("attempt count exceeds retry budget")
        if self.bytes_received and not (self.etag or self.last_modified):
            raise ValueError("resume requires ETag or Last-Modified validation")
        if self.state == "EXHAUSTED" and self.attempts_completed != self.max_attempts:
            raise ValueError("exhausted checkpoint must consume the retry budget")
        if self.state == "RETRY_WAIT" and (
            self.retry_not_before is None or not self.last_error
        ):
            raise ValueError("retry wait requires an error and next-attempt time")
        return self


class DownloadProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_key: Sha256
    provider: str
    endpoint: str
    parameters: tuple[tuple[str, str], ...]
    observed_at: datetime
    http_status: int = Field(ge=100, le=599, strict=True)
    etag: str | None = None
    last_modified: str | None = None

    @field_validator("observed_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("provenance time must use UTC")
        return value

    @model_validator(mode="after")
    def validate_request_identity(self) -> Self:
        task = build_download_task(
            provider=self.provider,
            endpoint=self.endpoint,
            parameters=self.parameters,
        )
        if task.cache_key != self.cache_key:
            raise ValueError("provenance does not match its request cache key")
        return self


class CacheCommit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content_sha256: Sha256
    byte_count: int = Field(ge=0, strict=True)
    relative_path: str
    duplicate_content: bool = Field(strict=True)
    provenance: DownloadProvenance


def commit_staged_download(
    *,
    staged_path: Path,
    cache_dir: Path,
    provenance: DownloadProvenance,
    expected_sha256: str | None = None,
) -> CacheCommit:
    """Verify and content-address a local staged file; performs no network operation."""

    if staged_path.is_symlink() or not staged_path.is_file():
        raise DownloadCacheError("staged download must be a regular file")
    content_hash = sha256_file(staged_path)
    if expected_sha256 is not None and content_hash != expected_sha256:
        raise DownloadCacheError("staged download checksum mismatch")
    relative = Path("blobs") / content_hash[:2] / content_hash
    destination = cache_dir / relative
    if not destination.resolve().is_relative_to(cache_dir.resolve()):
        raise DownloadCacheError("cache destination escapes cache root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    duplicate = destination.exists()
    if duplicate:
        if destination.is_symlink() or sha256_file(destination) != content_hash:
            raise DownloadCacheError("existing cache object failed checksum validation")
    else:
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        try:
            with staged_path.open("rb") as source, temporary.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                duplicate = True
                if destination.is_symlink() or sha256_file(destination) != content_hash:
                    raise DownloadCacheError(
                        "concurrent cache object failed checksum validation"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)
    return CacheCommit(
        content_sha256=content_hash,
        byte_count=staged_path.stat().st_size,
        relative_path=relative.as_posix(),
        duplicate_content=duplicate,
        provenance=provenance,
    )
