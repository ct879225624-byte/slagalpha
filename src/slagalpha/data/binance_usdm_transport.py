"""Bounded Binance USD-M public REST transport for authenticated P9 requirements."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import sha256_file
from slagalpha.data.download_cache import (
    DownloadProvenance,
    build_download_task,
    commit_staged_download,
)
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.market_data_requirements import (
    MarketDataRequirementPlan,
    build_market_data_requirement_plan,
)
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.replay_market_data import (
    MAX_RESPONSE_BYTES,
    ReplayMarketDataArtifact,
    ReplayRestResponse,
    build_replay_market_data_artifact,
    write_replay_market_data_artifact,
)
from slagalpha.research.replay_prep import (
    FundingScheduleArtifact,
    build_funding_schedule_artifact,
)

BASE_URL: Literal["https://fapi.binance.com"] = "https://fapi.binance.com"
PROVIDER: Literal["BINANCE_USDM"] = "BINANCE_USDM"
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
FUNDING_GUARD_HORIZON = timedelta(days=30)
TransportRole = Literal[
    "CANDLE_ONE_MINUTE",
    "FUNDING",
    "FUNDING_GUARD_BEFORE",
    "FUNDING_GUARD_AFTER",
]


class BinanceTransportError(ValueError):
    """The requested transport run cannot proceed or resume safely."""


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class BinanceTransportTask(BaseModel):
    """One deduplicated, accepted-request-bound public REST query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["binance-usdm-transport-task/0.2.0"] = (
        "binance-usdm-transport-task/0.2.0"
    )
    requirements_plan_hash: Sha256
    role: TransportRole
    provider: Literal["BINANCE_USDM"] = PROVIDER
    method: Literal["GET"] = "GET"
    base_url: Literal["https://fapi.binance.com"] = BASE_URL
    endpoint: Literal["/fapi/v1/klines", "/fapi/v1/fundingRate"]
    symbol: str
    interval: Literal["1m"] | None = None
    start_time_ms: int = Field(ge=0, strict=True)
    end_time_ms: int = Field(ge=0, strict=True)
    limit: int = Field(gt=0, strict=True)
    parameters: tuple[tuple[str, str], ...]
    accepted_request_hashes: tuple[Sha256, ...] = Field(min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)
    request_cache_key: Sha256
    task_hash: Sha256

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        if (
            not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or self.end_time_ms < self.start_time_ms
        ):
            raise ValueError("transport task symbol and time range are invalid")
        candle = self.role == "CANDLE_ONE_MINUTE"
        if (
            (candle and (self.endpoint, self.interval) != ("/fapi/v1/klines", "1m"))
            or (
                not candle
                and (self.endpoint, self.interval) != ("/fapi/v1/fundingRate", None)
            )
        ):
            raise ValueError("transport role and endpoint semantics disagree")
        expected_parameters = {
            "symbol": self.symbol,
            "startTime": str(self.start_time_ms),
            "endTime": str(self.end_time_ms),
            "limit": str(self.limit),
        }
        if candle:
            expected_parameters["interval"] = "1m"
        if self.parameters != tuple(sorted(expected_parameters.items())):
            raise ValueError("transport parameters do not match the bounded query")
        if self.accepted_request_hashes != tuple(
            sorted(set(self.accepted_request_hashes))
        ):
            raise ValueError("accepted request hashes must be unique and canonical")
        download = build_download_task(
            provider=self.provider,
            endpoint=self.endpoint,
            parameters=self.parameters,
            max_attempts=self.max_attempts,
        )
        if download.cache_key != self.request_cache_key:
            raise ValueError("transport request cache key mismatch")
        payload = self.model_dump(mode="json", exclude={"task_hash"})
        if self.task_hash != _hash(payload):
            raise ValueError("transport task hash mismatch")
        return self


class BinanceTransportPlan(BaseModel):
    """Self-authenticating query plan bound to one complete post-scan requirement plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["binance-usdm-transport-plan/0.2.0"] = (
        "binance-usdm-transport-plan/0.2.0"
    )
    requirements_plan_hash: Sha256
    source_report_hash: Sha256
    request_set_hash: Sha256
    accepted_request_hashes: tuple[Sha256, ...] = Field(min_length=1)
    tasks: tuple[BinanceTransportTask, ...] = Field(min_length=1)
    network_authorized: Literal[False] = False
    transport_plan_hash: Sha256

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.accepted_request_hashes != tuple(sorted(set(self.accepted_request_hashes))):
            raise ValueError("accepted request hashes must be unique and canonical")
        if self.tasks != tuple(sorted(self.tasks, key=lambda item: item.task_hash)):
            raise ValueError("transport tasks must be canonical")
        if len({item.task_hash for item in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate transport tasks are forbidden")
        if any(item.requirements_plan_hash != self.requirements_plan_hash for item in self.tasks):
            raise ValueError("transport task belongs to different requirements")
        known = set(self.accepted_request_hashes)
        if any(not set(item.accepted_request_hashes) <= known for item in self.tasks):
            raise ValueError("transport task escapes the accepted request set")
        intervals: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        for task in self.tasks:
            if task.role in {"CANDLE_ONE_MINUTE", "FUNDING"}:
                intervals[(task.role, task.symbol)].append(
                    (task.start_time_ms, task.end_time_ms)
                )
        if any(
            current_start <= previous_end
            for values in intervals.values()
            for (_, previous_end), (current_start, _) in zip(
                sorted(values), sorted(values)[1:], strict=False
            )
        ):
            raise ValueError("transport task ranges overlap")
        payload = self.model_dump(mode="json", exclude={"transport_plan_hash"})
        if self.transport_plan_hash != _hash(payload):
            raise ValueError("transport plan hash mismatch")
        return self


def _task(
    *,
    requirements_plan_hash: str,
    role: TransportRole,
    symbol: str,
    start: datetime,
    end_exclusive: datetime,
    request_hashes: tuple[str, ...],
    max_attempts: int,
) -> BinanceTransportTask:
    start_ms = _milliseconds(start)
    end_ms = _milliseconds(end_exclusive) - 1
    candle = role == "CANDLE_ONE_MINUTE"
    limit = (
        int((end_exclusive - start).total_seconds() // 60)
        if candle
        else (1 if role == "FUNDING_GUARD_AFTER" else 1000)
    )
    endpoint = "/fapi/v1/klines" if candle else "/fapi/v1/fundingRate"
    values = {
        "symbol": symbol,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(limit),
    }
    if candle:
        values["interval"] = "1m"
    parameters = tuple(sorted(values.items()))
    download = build_download_task(
        provider=PROVIDER,
        endpoint=endpoint,
        parameters=parameters,
        max_attempts=max_attempts,
    )
    payload = {
        "schema_version": "binance-usdm-transport-task/0.2.0",
        "requirements_plan_hash": requirements_plan_hash,
        "role": role,
        "provider": PROVIDER,
        "method": "GET",
        "base_url": BASE_URL,
        "endpoint": endpoint,
        "symbol": symbol,
        "interval": "1m" if candle else None,
        "start_time_ms": start_ms,
        "end_time_ms": end_ms,
        "limit": limit,
        "parameters": parameters,
        "accepted_request_hashes": tuple(sorted(request_hashes)),
        "max_attempts": max_attempts,
        "request_cache_key": download.cache_key,
    }
    return BinanceTransportTask.model_validate({**payload, "task_hash": _hash(payload)})


def _segments(
    report: DevSourceScanReport,
    requirements: MarketDataRequirementPlan,
) -> list[tuple[str, datetime, datetime, tuple[str, ...]]]:
    requests: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    for accepted in report.accepted_requests:
        request = accepted.request
        requests[request.request.armed.symbol].append(
            (request.start, request.end_exclusive, request.request_hash)
        )
    result: list[tuple[str, datetime, datetime, tuple[str, ...]]] = []
    for requirement in requirements.requirements:
        symbol_requests = requests[requirement.symbol]
        for window in requirement.windows:
            boundaries = {window.start, window.end_exclusive}
            for start, end, _ in symbol_requests:
                if start < window.end_exclusive and end > window.start:
                    boundaries.update((max(start, window.start), min(end, window.end_exclusive)))
            ordered = sorted(boundaries)
            for left, right in zip(ordered, ordered[1:], strict=False):
                cursor = left
                while cursor < right:
                    end = min(cursor + timedelta(minutes=1500), right)
                    hashes = tuple(
                        sorted(
                            request_hash
                            for start, request_end, request_hash in symbol_requests
                            if start < end and request_end > cursor
                        )
                    )
                    if not hashes:
                        raise BinanceTransportError("requirement segment has no accepted request")
                    result.append((requirement.symbol, cursor, end, hashes))
                    cursor = end
    return result


def build_binance_transport_plan(
    *,
    report: DevSourceScanReport,
    requirements: MarketDataRequirementPlan,
    max_attempts: int = 3,
) -> BinanceTransportPlan:
    """Authenticate requirements against their complete scan report and plan no-overlap queries."""

    report = DevSourceScanReport.model_validate(report.model_dump(mode="json"))
    requirements = MarketDataRequirementPlan.model_validate(
        requirements.model_dump(mode="json")
    )
    if report.status != "COMPLETE" or not report.accepted_requests:
        raise BinanceTransportError("transport requires a COMPLETE non-empty accepted request set")
    if build_market_data_requirement_plan(report) != requirements:
        raise BinanceTransportError(
            "requirements are not the authenticated post-scan output for this report"
        )
    accepted_hashes = tuple(
        sorted(item.request.request_hash for item in report.accepted_requests)
    )
    roles: tuple[TransportRole, ...] = (
        "CANDLE_ONE_MINUTE",
        "FUNDING",
    )
    tasks = [
        _task(
            requirements_plan_hash=requirements.plan_hash,
            role=role,
            symbol=symbol,
            start=start,
            end_exclusive=end,
            request_hashes=request_hashes,
            max_attempts=max_attempts,
        )
        for symbol, start, end, request_hashes in _segments(report, requirements)
        for role in roles
    ]
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for requirement in requirements.requirements:
        for window in requirement.windows:
            tasks.extend(
                (
                    _task(
                        requirements_plan_hash=requirements.plan_hash,
                        role="FUNDING_GUARD_BEFORE",
                        symbol=requirement.symbol,
                        start=max(epoch, window.start - FUNDING_GUARD_HORIZON),
                        end_exclusive=window.start,
                        request_hashes=window.request_hashes,
                        max_attempts=max_attempts,
                    ),
                    _task(
                        requirements_plan_hash=requirements.plan_hash,
                        role="FUNDING_GUARD_AFTER",
                        symbol=requirement.symbol,
                        start=window.end_exclusive,
                        end_exclusive=window.end_exclusive + FUNDING_GUARD_HORIZON,
                        request_hashes=window.request_hashes,
                        max_attempts=max_attempts,
                    ),
                )
            )
    ordered_tasks = tuple(
        sorted(
            tasks,
            key=lambda item: item.task_hash,
        )
    )
    payload = {
        "schema_version": "binance-usdm-transport-plan/0.2.0",
        "requirements_plan_hash": requirements.plan_hash,
        "source_report_hash": report.report_hash,
        "request_set_hash": report.request_set_hash,
        "accepted_request_hashes": accepted_hashes,
        "tasks": [item.model_dump(mode="json") for item in ordered_tasks],
        "network_authorized": False,
    }
    return BinanceTransportPlan.model_validate(
        {**payload, "transport_plan_hash": _hash(payload)}
    )


class TransportReceipt(BaseModel):
    """Immutable request/response provenance for one cached raw body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["binance-usdm-transport-receipt/0.1.0"] = (
        "binance-usdm-transport-receipt/0.1.0"
    )
    transport_plan_hash: Sha256
    task_hash: Sha256
    request_url: str
    observed_at: datetime
    http_status: Literal[200] = 200
    response_content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: Sha256
    byte_count: int = Field(ge=0, strict=True)
    blob_relative_path: str
    attempts_completed: int = Field(gt=0, strict=True)
    receipt_hash: Sha256

    @field_validator("observed_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("transport receipt time must use UTC")
        return value

    @field_validator("blob_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.parts or path.is_absolute() or str(path) != value or "\\" in value:
            raise ValueError("transport blob path must be project-relative")
        if any(part in {"..", "."} or ":" in part for part in path.parts):
            raise ValueError("transport blob path is unsafe")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if not self.request_url.startswith(f"{BASE_URL}/"):
            raise ValueError("transport receipt URL is outside Binance USD-M")
        payload = self.model_dump(mode="json", exclude={"receipt_hash"})
        if self.receipt_hash != _hash(payload):
            raise ValueError("transport receipt hash mismatch")
        return self


class TransportCheckpointEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_hash: Sha256
    attempts_completed: int = Field(ge=0, strict=True)
    status: Literal["PENDING", "COMPLETE", "FAILED", "EXHAUSTED"]
    receipt_hash: Sha256 | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        if (self.status == "COMPLETE") != (self.receipt_hash is not None):
            raise ValueError("completed task must have exactly one receipt")
        if self.status in {"FAILED", "EXHAUSTED"} and not self.last_error:
            raise ValueError("failed task must keep its error")
        return self


class TransportCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["binance-usdm-transport-checkpoint/0.1.0"] = (
        "binance-usdm-transport-checkpoint/0.1.0"
    )
    transport_plan_hash: Sha256
    entries: tuple[TransportCheckpointEntry, ...]
    checkpoint_hash: Sha256

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        if self.entries != tuple(sorted(self.entries, key=lambda item: item.task_hash)):
            raise ValueError("checkpoint entries must be canonical")
        if len({item.task_hash for item in self.entries}) != len(self.entries):
            raise ValueError("checkpoint task entries must be unique")
        payload = self.model_dump(mode="json", exclude={"checkpoint_hash"})
        if self.checkpoint_hash != _hash(payload):
            raise ValueError("transport checkpoint hash mismatch")
        return self


def _checkpoint(
    plan: BinanceTransportPlan, entries: list[TransportCheckpointEntry]
) -> TransportCheckpoint:
    payload = {
        "schema_version": "binance-usdm-transport-checkpoint/0.1.0",
        "transport_plan_hash": plan.transport_plan_hash,
        "entries": [
            item.model_dump(mode="json")
            for item in sorted(entries, key=lambda item: item.task_hash)
        ],
    }
    return TransportCheckpoint.model_validate({**payload, "checkpoint_hash": _hash(payload)})


def _write_mutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_checkpoint(path: Path, checkpoint: TransportCheckpoint) -> None:
    _write_mutable(path, canonical_json_bytes(checkpoint.model_dump(mode="json")))


def _load_receipt(path: Path, *, task: BinanceTransportTask, project_dir: Path) -> TransportReceipt:
    receipt = TransportReceipt.model_validate_json(path.read_bytes())
    blob = project_dir / PurePosixPath(receipt.blob_relative_path)
    if (
        receipt.task_hash != task.task_hash
        or blob.is_symlink()
        or not blob.is_file()
        or sha256_file(blob) != receipt.body_sha256
        or blob.stat().st_size != receipt.byte_count
    ):
        raise BinanceTransportError("saved transport receipt or cache blob failed validation")
    return receipt


def write_binance_transport_plan(plan: BinanceTransportPlan, data_dir: Path) -> Path:
    plan = BinanceTransportPlan.model_validate(plan.model_dump(mode="json"))
    destination = data_dir / "transport_plans" / f"{plan.transport_plan_hash}.json"
    content = canonical_json_bytes(plan.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise BinanceTransportError("existing transport plan changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination


def _response_error(response: httpx.Response, task: BinanceTransportTask) -> str | None:
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    if len(response.content) > MAX_RESPONSE_BYTES:
        return "response exceeds replay raw-body size limit"
    try:
        payload = json.loads(response.content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "response is not valid JSON"
    if not isinstance(payload, list) or len(payload) > task.limit:
        return "response is not a bounded JSON array"
    if task.role in {"FUNDING", "FUNDING_GUARD_BEFORE"} and len(payload) >= task.limit:
        return "Funding response may be truncated at limit"
    return None


def run_binance_transport(
    *,
    project_dir: Path,
    data_dir: Path,
    plan: BinanceTransportPlan,
    approved_requirements_plan_hash: str,
    client: httpx.Client,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> TransportCheckpoint:
    """Execute or resume one explicitly approved plan; successful tasks are never repeated."""

    project_dir = project_dir.resolve()
    data_dir = data_dir.resolve()
    plan = BinanceTransportPlan.model_validate(plan.model_dump(mode="json"))
    if approved_requirements_plan_hash != plan.requirements_plan_hash:
        raise BinanceTransportError("owner approval does not match the requirements hash")
    if not data_dir.is_relative_to(project_dir) or data_dir == project_dir:
        raise BinanceTransportError("transport data directory must be inside the project")
    if project_dir.is_symlink() or data_dir.is_symlink():
        raise BinanceTransportError("transport roots cannot be symlinks")
    run_dir = data_dir / "transport_runs" / plan.transport_plan_hash
    receipts_dir = run_dir / "receipts"
    staging_dir = run_dir / "staging"
    checkpoint_path = run_dir / "checkpoint.json"
    write_binance_transport_plan(plan, data_dir)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = data_dir / "cache"

    if checkpoint_path.exists():
        checkpoint = TransportCheckpoint.model_validate_json(checkpoint_path.read_bytes())
        if checkpoint.transport_plan_hash != plan.transport_plan_hash:
            raise BinanceTransportError("checkpoint belongs to different requirements")
        entries = {item.task_hash: item for item in checkpoint.entries}
        if set(entries) != {item.task_hash for item in plan.tasks}:
            raise BinanceTransportError("checkpoint task set differs from transport plan")
    else:
        entries = {
            task.task_hash: TransportCheckpointEntry(
                task_hash=task.task_hash, attempts_completed=0, status="PENDING"
            )
            for task in plan.tasks
        }

    for task in plan.tasks:
        receipt_path = receipts_dir / f"{task.task_hash}.json"
        entry = entries[task.task_hash]
        if receipt_path.exists():
            receipt = _load_receipt(receipt_path, task=task, project_dir=project_dir)
            entries[task.task_hash] = TransportCheckpointEntry(
                task_hash=task.task_hash,
                attempts_completed=max(entry.attempts_completed, receipt.attempts_completed),
                status="COMPLETE",
                receipt_hash=receipt.receipt_hash,
            )
            continue
        if entry.status == "COMPLETE":
            raise BinanceTransportError("completed checkpoint is missing its immutable receipt")
        if entry.status in {"FAILED", "EXHAUSTED"}:
            raise BinanceTransportError(
                f"transport task {task.task_hash} previously failed: {entry.last_error}"
            )

        attempts = entry.attempts_completed
        while attempts < task.max_attempts:
            attempts += 1
            response: httpx.Response | None = None
            try:
                response = client.get(
                    f"{task.base_url}{task.endpoint}", params=dict(task.parameters)
                )
                error = _response_error(response, task)
            except httpx.TransportError as exc:
                error = f"{type(exc).__name__}: {exc}"
            retryable = response is None or response.status_code in RETRYABLE_STATUSES
            if error is not None:
                terminal = not retryable or attempts == task.max_attempts
                status: Literal["PENDING", "FAILED", "EXHAUSTED"]
                status = (
                    "FAILED"
                    if not retryable
                    else ("EXHAUSTED" if terminal else "PENDING")
                )
                entries[task.task_hash] = TransportCheckpointEntry(
                    task_hash=task.task_hash,
                    attempts_completed=attempts,
                    status=status,
                    last_error=error,
                )
                _write_checkpoint(checkpoint_path, _checkpoint(plan, list(entries.values())))
                if terminal:
                    raise BinanceTransportError(
                        f"transport task {task.task_hash} failed after "
                        f"{attempts} attempt(s): {error}"
                    )
                sleep(min(2 ** (attempts - 1), 8))
                continue

            assert response is not None
            observed_at = now()
            if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
                raise BinanceTransportError("transport clock must return UTC")
            staged = staging_dir / f"{task.task_hash}.part"
            staged.unlink(missing_ok=True)
            staged.write_bytes(response.content)
            provenance = DownloadProvenance(
                cache_key=task.request_cache_key,
                provider=task.provider,
                endpoint=task.endpoint,
                parameters=task.parameters,
                observed_at=observed_at,
                http_status=response.status_code,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
            commit = commit_staged_download(
                staged_path=staged, cache_dir=cache_dir, provenance=provenance
            )
            staged.unlink(missing_ok=True)
            blob = cache_dir / PurePosixPath(commit.relative_path)
            relative_blob = blob.relative_to(project_dir).as_posix()
            receipt_payload = {
                "schema_version": "binance-usdm-transport-receipt/0.1.0",
                "transport_plan_hash": plan.transport_plan_hash,
                "task_hash": task.task_hash,
                "request_url": str(response.request.url),
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "http_status": 200,
                "response_content_type": response.headers.get("content-type"),
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "body_sha256": commit.content_sha256,
                "byte_count": commit.byte_count,
                "blob_relative_path": relative_blob,
                "attempts_completed": attempts,
            }
            receipt = TransportReceipt.model_validate(
                {**receipt_payload, "receipt_hash": _hash(receipt_payload)}
            )
            _publish_immutable(
                receipt_path, canonical_json_bytes(receipt.model_dump(mode="json"))
            )
            entries[task.task_hash] = TransportCheckpointEntry(
                task_hash=task.task_hash,
                attempts_completed=attempts,
                status="COMPLETE",
                receipt_hash=receipt.receipt_hash,
            )
            _write_checkpoint(checkpoint_path, _checkpoint(plan, list(entries.values())))
            break

    checkpoint = _checkpoint(plan, list(entries.values()))
    if any(item.status != "COMPLETE" for item in checkpoint.entries):
        raise BinanceTransportError("transport stopped with incomplete tasks")
    _write_checkpoint(checkpoint_path, checkpoint)
    return checkpoint


class BinanceTransportOutputs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    market_data: tuple[ReplayMarketDataArtifact, ...]
    funding_schedules: tuple[FundingScheduleArtifact, ...]


def _saved_responses(
    *, project_dir: Path, data_dir: Path, plan: BinanceTransportPlan
) -> dict[str, ReplayRestResponse]:
    result: dict[str, ReplayRestResponse] = {}
    receipts_dir = data_dir / "transport_runs" / plan.transport_plan_hash / "receipts"
    for task in plan.tasks:
        receipt = _load_receipt(
            receipts_dir / f"{task.task_hash}.json", task=task, project_dir=project_dir
        )
        result[task.task_hash] = ReplayRestResponse(
            endpoint=task.endpoint,
            symbol=task.symbol,
            interval=task.interval,
            start_time_ms=task.start_time_ms,
            end_time_ms=task.end_time_ms,
            limit=task.limit,
            observed_at=receipt.observed_at,
            relative_path=receipt.blob_relative_path,
            body_sha256=receipt.body_sha256,
        )
    return result


def _schedule_times(
    *,
    project_dir: Path,
    tasks: tuple[BinanceTransportTask, ...],
    responses: dict[str, ReplayRestResponse],
) -> tuple[datetime, ...]:
    times: list[datetime] = []
    for task in tasks:
        response = responses[task.task_hash]
        payload = json.loads((project_dir / response.relative_path).read_bytes())
        if not isinstance(payload, list):
            raise BinanceTransportError("Funding schedule source is not an array")
        for row in payload:
            if not isinstance(row, dict) or row.get("symbol") != task.symbol:
                raise BinanceTransportError("Funding schedule source has wrong symbol or shape")
            value = row.get("fundingTime")
            if type(value) is not int or not task.start_time_ms <= value <= task.end_time_ms:
                raise BinanceTransportError("Funding schedule source has invalid settlement time")
            times.append(datetime.fromtimestamp(value / 1000, UTC))
    if times != sorted(set(times)):
        raise BinanceTransportError("Funding schedule source is duplicated or disordered")
    return tuple(times)


def finalize_binance_transport(
    *,
    project_dir: Path,
    data_dir: Path,
    report: DevSourceScanReport,
    requirements: MarketDataRequirementPlan,
    plan: BinanceTransportPlan,
) -> BinanceTransportOutputs:
    """Delegate raw-body validation to replay_market_data and write versioned artifacts."""

    project_dir = project_dir.resolve()
    data_dir = data_dir.resolve()
    authenticated = build_binance_transport_plan(
        report=report,
        requirements=requirements,
        max_attempts=plan.tasks[0].max_attempts,
    )
    plan = BinanceTransportPlan.model_validate(plan.model_dump(mode="json"))
    if authenticated != plan:
        raise BinanceTransportError("transport plan no longer matches authenticated inputs")
    checkpoint_path = data_dir / "transport_runs" / plan.transport_plan_hash / "checkpoint.json"
    checkpoint = TransportCheckpoint.model_validate_json(checkpoint_path.read_bytes())
    if (
        checkpoint.transport_plan_hash != plan.transport_plan_hash
        or any(item.status != "COMPLETE" for item in checkpoint.entries)
    ):
        raise BinanceTransportError("transport cannot finalize an incomplete run")
    checkpoint_entries = {item.task_hash: item for item in checkpoint.entries}
    if set(checkpoint_entries) != {item.task_hash for item in plan.tasks}:
        raise BinanceTransportError("checkpoint task set differs from transport plan")
    receipts_dir = data_dir / "transport_runs" / plan.transport_plan_hash / "receipts"
    for task in plan.tasks:
        receipt = _load_receipt(
            receipts_dir / f"{task.task_hash}.json",
            task=task,
            project_dir=project_dir,
        )
        if checkpoint_entries[task.task_hash].receipt_hash != receipt.receipt_hash:
            raise BinanceTransportError("checkpoint and immutable receipt disagree")
    responses = _saved_responses(project_dir=project_dir, data_dir=data_dir, plan=plan)

    schedules: list[FundingScheduleArtifact] = []
    schedule_for_window: dict[tuple[str, int, int], FundingScheduleArtifact] = {}
    for requirement in requirements.requirements:
        for window in requirement.windows:
            start_ms = _milliseconds(window.start)
            end_ms = _milliseconds(window.end_exclusive) - 1
            tasks = tuple(
                sorted(
                    (
                        item
                        for item in plan.tasks
                        if item.role == "FUNDING"
                        and item.symbol == requirement.symbol
                        and start_ms <= item.start_time_ms
                        and item.end_time_ms <= end_ms
                    ),
                    key=lambda item: item.start_time_ms,
                )
            )
            guard_before = tuple(
                item
                for item in plan.tasks
                if item.role == "FUNDING_GUARD_BEFORE"
                and item.symbol == requirement.symbol
                and item.end_time_ms == start_ms - 1
                and item.accepted_request_hashes == window.request_hashes
            )
            guard_after = tuple(
                item
                for item in plan.tasks
                if item.role == "FUNDING_GUARD_AFTER"
                and item.symbol == requirement.symbol
                and item.start_time_ms == end_ms + 1
                and item.accepted_request_hashes == window.request_hashes
            )
            if len(guard_before) != 1 or len(guard_after) != 1:
                raise BinanceTransportError(
                    "Funding schedule requires one guard query on each boundary"
                )
            times = _schedule_times(
                project_dir=project_dir, tasks=tasks, responses=responses
            )
            before_times = _schedule_times(
                project_dir=project_dir, tasks=guard_before, responses=responses
            )
            after_times = _schedule_times(
                project_dir=project_dir, tasks=guard_after, responses=responses
            )
            if not before_times or len(after_times) != 1:
                raise BinanceTransportError(
                    "Funding guard did not return adjacent observed settlement evidence"
                )
            adjacent_times = (before_times[-1], *times, after_times[0])
            if adjacent_times != tuple(sorted(set(adjacent_times))):
                raise BinanceTransportError(
                    "Funding guard and window settlements are not strictly ordered"
                )
            evidence_tasks = (*guard_before, *tasks, *guard_after)
            receipt_hashes = [
                TransportReceipt.model_validate_json(
                    (
                        data_dir
                        / "transport_runs"
                        / plan.transport_plan_hash
                        / "receipts"
                        / f"{item.task_hash}.json"
                    ).read_bytes()
                ).receipt_hash
                for item in evidence_tasks
            ]
            source_hash = _hash({"receipt_hashes": receipt_hashes})
            schedule = build_funding_schedule_artifact(
                symbol=requirement.symbol,
                coverage_start=window.start,
                coverage_end_exclusive=window.end_exclusive,
                settlement_times=times,
                provider=PROVIDER,
                schedule_version="binance-usdm-funding-history-observed-events/2",
                source_ref=(
                    f"transport:{plan.transport_plan_hash}:"
                    f"{requirement.symbol}:{start_ms}:{end_ms}"
                ),
                source_content_sha256=source_hash,
                observed_at=max(
                    responses[item.task_hash].observed_at for item in evidence_tasks
                ),
            )
            destination = (
                data_dir / "manifests" / "funding_schedule" / f"{schedule.schedule_hash}.json"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _publish_immutable(
                destination, canonical_json_bytes(schedule.model_dump(mode="json"))
            )
            schedules.append(schedule)
            schedule_for_window[(requirement.symbol, start_ms, end_ms)] = schedule

    artifacts: list[ReplayMarketDataArtifact] = []
    report = DevSourceScanReport.model_validate(report.model_dump(mode="json"))
    for accepted in report.accepted_requests:
        request = accepted.request
        symbol = request.request.armed.symbol
        start_ms = _milliseconds(request.start)
        end_ms = _milliseconds(request.end_exclusive) - 1
        for role in ("CANDLE_ONE_MINUTE", "FUNDING"):
            tasks = tuple(
                sorted(
                    (
                        item
                        for item in plan.tasks
                        if item.role == role
                        and item.symbol == symbol
                        and request.request_hash in item.accepted_request_hashes
                        and start_ms <= item.start_time_ms
                        and item.end_time_ms <= end_ms
                    ),
                    key=lambda item: item.start_time_ms,
                )
            )
            saved = tuple(responses[item.task_hash] for item in tasks)
            if role == "FUNDING":
                schedule = next(
                    item
                    for (item_symbol, left, right), item in schedule_for_window.items()
                    if item_symbol == symbol and left <= start_ms and right >= end_ms
                )
                required = tuple(
                    value
                    for value in schedule.settlement_times
                    if request.start < value < request.end_exclusive - timedelta(minutes=1)
                )
                if not required:
                    continue
            artifact, _ = build_replay_market_data_artifact(
                project_dir=project_dir, request=request, role=role, responses=saved
            )
            write_replay_market_data_artifact(artifact, data_dir)
            artifacts.append(artifact)
    return BinanceTransportOutputs(
        market_data=tuple(sorted(artifacts, key=lambda item: (item.request_hash, item.role))),
        funding_schedules=tuple(sorted(schedules, key=lambda item: item.schedule_hash)),
    )
