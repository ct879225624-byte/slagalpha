"""Resumable full-period daily Universe construction over normalized archives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.klines import ArchiveNormalizationManifest
from slagalpha.data.universe import (
    REQUIRED_INTERVALS,
    UNIVERSE_SELECTION_VERSION,
    build_daily_universe,
    write_universe_snapshot,
)
from slagalpha.domain.universe import (
    ContractIdentityRegistry,
    ExclusionLedger,
    UniverseBlockReason,
    UniverseSnapshot,
)


class UniverseBatchError(RuntimeError):
    """Raised when full-period Universe inputs or immutable outputs conflict."""


class UniverseDayFailure(BaseModel):
    """One day that could not produce a snapshot due to a global error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selection_date: date
    error: str


class UniverseBatchResult(BaseModel):
    """Deterministic aggregate receipt for a daily Universe run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["universe-batch/0.1.0"] = "universe-batch/0.1.0"
    run_version: str
    progress_version: str
    dataset_content_hash: str
    registry_version: str
    exclusion_ledger_version: str
    selection_start: date
    selection_end_exclusive: date
    expected_count: int = Field(ge=0)
    computed_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    total_member_count: int = Field(ge=0)
    total_blocked_count: int = Field(ge=0)
    daily_snapshot_hash: str
    failures: tuple[UniverseDayFailure, ...]
    complete: bool
    result_hash: str

    @field_validator(
        "run_version",
        "progress_version",
        "dataset_content_hash",
        "daily_snapshot_hash",
        "result_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("Universe batch hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.selection_end_exclusive < self.selection_start:
            raise ValueError("selection_end_exclusive must not precede selection_start")
        if self.computed_count + self.reused_count + self.failed_count != self.expected_count:
            raise ValueError("Universe batch counts do not reconcile")
        if self.failed_count != len(self.failures):
            raise ValueError("failed_count does not match failures")
        failure_dates = tuple(item.selection_date for item in self.failures)
        if failure_dates != tuple(sorted(set(failure_dates))):
            raise ValueError("Universe failures must be unique and canonical")
        if self.complete != (self.failed_count == 0):
            raise ValueError("complete does not match failed_count")
        return self


def _canonical_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _require_sha256(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise UniverseBatchError(f"{field} must be SHA-256")
    return normalized


def _atomic_write(destination: Path, content: bytes) -> Path:
    if destination.exists():
        if destination.read_bytes() != content:
            raise UniverseBatchError(
                f"existing content-addressed batch file changed: {destination}"
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _manifest_index(
    manifests_dir: Path,
) -> dict[tuple[str, str, str], ArchiveNormalizationManifest]:
    index: dict[tuple[str, str, str], ArchiveNormalizationManifest] = {}
    root = manifests_dir / "archive_normalization"
    for path in root.glob("*/*/*.json"):
        manifest = ArchiveNormalizationManifest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        key = (manifest.symbol, manifest.interval, manifest.period)
        existing = index.get(key)
        if existing is not None and existing != manifest:
            raise UniverseBatchError(f"conflicting normalization manifests for {key}")
        index[key] = manifest
    return index


def _period_ranking_frames(
    *,
    period: str,
    symbols: tuple[str, ...],
    manifest_index: Mapping[tuple[str, str, str], ArchiveNormalizationManifest],
    normalized_data_dir: Path,
) -> tuple[dict[date, dict[str, pd.DataFrame]], dict[str, tuple[UniverseBlockReason, ...]]]:
    frames_by_day: dict[date, dict[str, pd.DataFrame]] = {}
    load_blocks: dict[str, tuple[UniverseBlockReason, ...]] = {}
    columns = (
        "open_time",
        "close_time_exclusive",
        "quote_volume",
        "is_closed",
        "source_file_hash",
    )
    for symbol in symbols:
        manifest = manifest_index.get((symbol, "15m", period))
        if manifest is None:
            continue
        try:
            frame = pd.read_parquet(
                normalized_data_dir / manifest.output_relative_path,
                columns=list(columns),
            )
            open_times = pd.to_datetime(frame["open_time"], utc=True)
            frame = frame.assign(open_time=open_times, _ranking_day=open_times.dt.date)
            for ranking_day, group in frame.groupby("_ranking_day", sort=True):
                day = ranking_day
                if not isinstance(day, date):
                    raise UniverseBatchError("ranking day is not a date")
                frames_by_day.setdefault(day, {})[symbol] = group.drop(
                    columns="_ranking_day"
                ).reset_index(drop=True)
        except Exception:  # noqa: BLE001 - isolate one unreadable symbol-period
            load_blocks[symbol] = (UniverseBlockReason.DATA_INCOMPLETE,)
    return frames_by_day, load_blocks


def _period_evidence(
    *,
    period: str,
    symbols: tuple[str, ...],
    manifest_index: Mapping[tuple[str, str, str], ArchiveNormalizationManifest],
) -> dict[str, dict[str, str]]:
    return {
        symbol: {
            interval: manifest.normalized_content_hash
            for interval in REQUIRED_INTERVALS
            if (manifest := manifest_index.get((symbol, interval, period))) is not None
        }
        for symbol in symbols
    }


def _selected_at(selection_date: date) -> datetime:
    return datetime.combine(selection_date, time(hour=0, minute=5), tzinfo=UTC)


def _progress_content(
    run_version: str,
    selection_date: date,
    snapshot: UniverseSnapshot,
) -> bytes:
    payload = {
        "run_version": run_version,
        "selection_date": selection_date.isoformat(),
        "universe_version": snapshot.universe_version,
    }
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _reuse_snapshot(
    *,
    progress_path: Path,
    snapshots_dir: Path,
    run_version: str,
    selection_date: date,
    registry_version: str,
    ledger_version: str,
) -> UniverseSnapshot:
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        universe_version = str(progress["universe_version"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise UniverseBatchError(f"invalid progress receipt: {progress_path}") from error
    if progress.get("run_version") != run_version:
        raise UniverseBatchError("progress receipt run_version mismatch")
    snapshot_path = snapshots_dir / f"{universe_version}.json"
    try:
        snapshot = UniverseSnapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise UniverseBatchError(f"invalid persisted snapshot: {snapshot_path}") from error
    if (
        snapshot.selected_at != _selected_at(selection_date)
        or snapshot.contract_registry_version != registry_version
        or snapshot.exclusion_ledger_version != ledger_version
        or progress.get("selection_date") != selection_date.isoformat()
    ):
        raise UniverseBatchError("persisted Universe snapshot context mismatch")
    return snapshot


def run_universe_batch(
    *,
    registry: ContractIdentityRegistry,
    exclusion_ledger: ExclusionLedger,
    dataset_content_hash: str,
    selection_start: date,
    selection_end_exclusive: date,
    normalized_data_dir: Path,
    manifests_dir: Path,
    reuse_progress: bool = True,
    progress_namespace: str | None = None,
) -> UniverseBatchResult:
    """Build every daily snapshot in a half-open date range and persist progress."""

    if selection_end_exclusive < selection_start:
        raise UniverseBatchError("selection_end_exclusive precedes selection_start")
    dataset_hash = _require_sha256(dataset_content_hash, "dataset_content_hash")
    run_payload = {
        "selection_algorithm_version": UNIVERSE_SELECTION_VERSION,
        "registry_version": registry.registry_version,
        "exclusion_ledger_version": exclusion_ledger.ledger_version,
        "dataset_content_hash": dataset_hash,
        "selection_start": selection_start.isoformat(),
        "selection_end_exclusive": selection_end_exclusive.isoformat(),
    }
    run_version = _canonical_sha256(run_payload)
    if progress_namespace is None:
        progress_version = run_version
    else:
        namespace = progress_namespace.strip()
        if not namespace:
            raise UniverseBatchError("progress_namespace must not be blank")
        progress_version = _canonical_sha256(
            {"run_version": run_version, "progress_namespace": namespace}
        )
    index = _manifest_index(manifests_dir)
    symbols = tuple(entry.symbol for entry in registry.entries)
    progress_dir = manifests_dir / "universe_batch_progress" / progress_version
    snapshots_dir = manifests_dir / "universe_snapshot"

    computed_count = 0
    reused_count = 0
    total_member_count = 0
    total_blocked_count = 0
    failures: list[UniverseDayFailure] = []
    snapshot_versions: list[dict[str, str]] = []
    cached_period = ""
    frames_by_day: dict[date, dict[str, pd.DataFrame]] = {}
    load_blocks: dict[str, tuple[UniverseBlockReason, ...]] = {}
    evidence: dict[str, dict[str, str]] = {}

    expected_count = (selection_end_exclusive - selection_start).days
    for offset in range(expected_count):
        selection_date = selection_start + timedelta(days=offset)
        progress_path = progress_dir / f"{selection_date.isoformat()}.json"
        try:
            if reuse_progress and progress_path.is_file():
                snapshot = _reuse_snapshot(
                    progress_path=progress_path,
                    snapshots_dir=snapshots_dir,
                    run_version=progress_version,
                    selection_date=selection_date,
                    registry_version=registry.registry_version,
                    ledger_version=exclusion_ledger.ledger_version,
                )
                reused_count += 1
            else:
                ranking_day = selection_date - timedelta(days=1)
                period = ranking_day.strftime("%Y-%m")
                if period != cached_period:
                    frames_by_day, load_blocks = _period_ranking_frames(
                        period=period,
                        symbols=symbols,
                        manifest_index=index,
                        normalized_data_dir=normalized_data_dir,
                    )
                    evidence = _period_evidence(
                        period=period,
                        symbols=symbols,
                        manifest_index=index,
                    )
                    cached_period = period
                snapshot = build_daily_universe(
                    selected_at=_selected_at(selection_date),
                    registry=registry,
                    exclusion_ledger=exclusion_ledger,
                    fifteen_minute_candles=frames_by_day.get(ranking_day, {}),
                    interval_evidence=evidence,
                    blocking_reasons=load_blocks,
                )
                write_universe_snapshot(snapshot, manifests_dir.parent)
                _atomic_write(
                    progress_path,
                    _progress_content(progress_version, selection_date, snapshot),
                )
                computed_count += 1
            total_member_count += snapshot.member_count
            total_blocked_count += len(snapshot.blocked_candidates)
            snapshot_versions.append(
                {
                    "selection_date": selection_date.isoformat(),
                    "universe_version": snapshot.universe_version,
                }
            )
        except Exception as error:  # noqa: BLE001 - one broken day must not stop the batch
            failures.append(
                UniverseDayFailure(
                    selection_date=selection_date,
                    error=f"{type(error).__name__}: {error}",
                )
            )

    daily_snapshot_hash = _canonical_sha256(snapshot_versions)
    semantic_result = {
        **run_payload,
        "run_version": run_version,
        "progress_version": progress_version,
        "expected_count": expected_count,
        "computed_count": computed_count,
        "reused_count": reused_count,
        "failed_count": len(failures),
        "total_member_count": total_member_count,
        "total_blocked_count": total_blocked_count,
        "daily_snapshot_hash": daily_snapshot_hash,
        "failures": [failure.model_dump(mode="json") for failure in failures],
        "complete": not failures,
    }
    result_hash = _canonical_sha256(semantic_result)
    result = UniverseBatchResult(
        run_version=run_version,
        progress_version=progress_version,
        dataset_content_hash=dataset_hash,
        registry_version=registry.registry_version,
        exclusion_ledger_version=exclusion_ledger.ledger_version,
        selection_start=selection_start,
        selection_end_exclusive=selection_end_exclusive,
        expected_count=expected_count,
        computed_count=computed_count,
        reused_count=reused_count,
        failed_count=len(failures),
        total_member_count=total_member_count,
        total_blocked_count=total_blocked_count,
        daily_snapshot_hash=daily_snapshot_hash,
        failures=tuple(failures),
        complete=not failures,
        result_hash=result_hash,
    )
    result_path = manifests_dir / "universe_batch" / f"{result_hash}.json"
    content = (
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    _atomic_write(result_path, content)
    return result
