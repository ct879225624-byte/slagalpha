"""Orchestrate one public archive through verified normalized storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from slagalpha.config import Settings
from slagalpha.data.archive import (
    ArchiveDownloadManifest,
    ArchiveSpec,
    fetch_archive,
    write_download_manifest,
)
from slagalpha.data.klines import (
    ArchiveNormalizationManifest,
    normalize_archive_to_parquet,
    write_normalization_manifest,
)


@dataclass(frozen=True, slots=True)
class ArchiveIngestionResult:
    """Paths and immutable evidence emitted by one ingestion."""

    archive_path: Path
    download_manifest_path: Path
    parquet_path: Path
    normalization_manifest_path: Path
    download: ArchiveDownloadManifest
    normalization: ArchiveNormalizationManifest

    def public_dict(self) -> dict[str, str | int]:
        return {
            "archive_path": str(self.archive_path),
            "download_manifest_path": str(self.download_manifest_path),
            "parquet_path": str(self.parquet_path),
            "normalization_manifest_path": str(self.normalization_manifest_path),
            "source_sha256": self.download.actual_sha256,
            "normalized_content_hash": self.normalization.normalized_content_hash,
            "rows": self.normalization.normalized_row_count,
        }


def ingest_monthly_archive(
    spec: ArchiveSpec,
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    evaluated_at: datetime | None = None,
    require_full_period: bool = False,
) -> ArchiveIngestionResult:
    """Download, verify, normalize, and record one monthly archive."""

    archive, download = fetch_archive(
        spec,
        settings.data_dir / "raw",
        client=client,
    )
    download_manifest_path = write_download_manifest(
        download, settings.data_dir / "manifests"
    )
    parquet, normalization = normalize_archive_to_parquet(
        archive,
        download,
        spec,
        settings.data_dir / "normalized",
        evaluated_at=evaluated_at,
        require_full_period=require_full_period,
    )
    normalization_manifest_path = write_normalization_manifest(
        normalization, settings.data_dir / "manifests"
    )
    return ArchiveIngestionResult(
        archive_path=archive,
        download_manifest_path=download_manifest_path,
        parquet_path=parquet,
        normalization_manifest_path=normalization_manifest_path,
        download=download,
        normalization=normalization,
    )
