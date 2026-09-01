"""Command-line entry point for local validation and future research commands."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from slagalpha import __version__
from slagalpha.config import Settings
from slagalpha.data.archive import ArchiveError, ArchiveSpec, sha256_file
from slagalpha.data.archive_batch import (
    ArchiveBatchError,
    download_capacity_plan,
    write_archive_batch_result,
)
from slagalpha.data.capacity import (
    ArchiveCapacityError,
    ArchiveCapacityEstimate,
    estimate_archive_capacity,
    write_archive_capacity_artifacts,
)
from slagalpha.data.ingestion import ingest_monthly_archive
from slagalpha.data.inventory import (
    MONTHLY_KLINE_PREFIX,
    ArchiveInventory,
    ArchiveInventoryError,
    fetch_archive_inventory,
    plan_monthly_archives,
    write_archive_inventory_manifest,
    write_archive_plan_manifest,
)
from slagalpha.data.klines import KlineArchiveError
from slagalpha.data.normalization_batch import (
    NormalizationBatchError,
    normalize_capacity_plan,
    write_normalization_batch_result,
)
from slagalpha.logging_config import configure_logging


def _month(value: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}", value) is None:
        raise argparse.ArgumentTypeError("month must use zero-padded YYYY-MM")
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError as error:
        raise argparse.ArgumentTypeError("month must use YYYY-MM") from error
    return parsed


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("value must be a decimal") from error
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser without performing configuration or I/O."""

    parser = argparse.ArgumentParser(
        prog="slagalpha",
        description="Deterministic Crypto Trading Copilot research tools.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Print the package version.")
    subparsers.add_parser(
        "config-check",
        help="Validate and print the secret-free V0.1 configuration.",
    )
    archive_parser = subparsers.add_parser(
        "archive-ingest",
        help="Download and normalize one Binance USD-M monthly kline archive.",
    )
    archive_parser.add_argument("--symbol", required=True)
    archive_parser.add_argument(
        "--interval",
        required=True,
        choices=["1m", "15m", "1h", "4h", "1d"],
    )
    archive_parser.add_argument("--year", required=True, type=int)
    archive_parser.add_argument("--month", required=True, type=int)
    archive_parser.add_argument(
        "--require-full-period",
        action="store_true",
        help="Reject an archive that does not cover the complete calendar month.",
    )
    plan_parser = subparsers.add_parser(
        "archive-plan",
        help="Snapshot official inventory and plan monthly archives without downloading ZIPs.",
    )
    plan_parser.add_argument("--symbol", required=True)
    plan_parser.add_argument(
        "--interval",
        required=True,
        choices=["1m", "15m", "1h", "4h", "1d"],
    )
    plan_parser.add_argument("--start", required=True, type=_month)
    plan_parser.add_argument("--end-exclusive", required=True, type=_month)
    capacity_parser = subparsers.add_parser(
        "archive-capacity",
        help="Calculate an exact no-download capacity gate from official S3 listings.",
    )
    capacity_parser.add_argument(
        "--root-inventory-manifest",
        required=True,
        type=Path,
        help="Content-addressed root archive inventory JSON.",
    )
    capacity_parser.add_argument("--exchange-info-hash", required=True)
    capacity_parser.add_argument("--start", required=True, type=_month)
    capacity_parser.add_argument("--end-exclusive", required=True, type=_month)
    capacity_parser.add_argument("--max-workers", type=int, default=16)
    capacity_parser.add_argument(
        "--projection-multiplier",
        type=_positive_decimal,
        default=Decimal("4"),
    )
    capacity_parser.add_argument(
        "--threshold-gib",
        type=_positive_decimal,
        default=Decimal("20"),
    )
    download_parser = subparsers.add_parser(
        "archive-download-plan",
        help="Download and verify every object in an accepted capacity plan.",
    )
    download_parser.add_argument(
        "--capacity-manifest",
        required=True,
        type=Path,
    )
    download_parser.add_argument("--max-workers", type=int, default=16)
    normalize_parser = subparsers.add_parser(
        "archive-normalize-plan",
        help="Validate and normalize every downloaded archive in a capacity plan.",
    )
    normalize_parser.add_argument(
        "--capacity-manifest",
        required=True,
        type=Path,
    )
    normalize_parser.add_argument("--max-workers", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "version":
        print(__version__)
        return 0

    if args.command == "config-check":
        try:
            settings = Settings.from_env()
        except ValidationError as error:
            print(f"configuration invalid: {error}", file=sys.stderr)
            return 2

        configure_logging(settings.log_level)
        logging.getLogger("slagalpha.cli").info(
            "configuration is valid",
            extra={
                "event": "configuration_valid",
                "environment": settings.environment.value,
            },
        )
        print(json.dumps(settings.public_dict(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "archive-ingest":
        try:
            settings = Settings.from_env()
            spec = ArchiveSpec(
                symbol=args.symbol,
                interval=args.interval,
                year=args.year,
                month=args.month,
            )
            configure_logging(settings.log_level)
            ingestion_result = ingest_monthly_archive(
                spec,
                settings,
                require_full_period=args.require_full_period,
            )
        except (ValidationError, ArchiveError, KlineArchiveError, OSError) as error:
            print(f"archive ingestion failed: {error}", file=sys.stderr)
            return 1

        logging.getLogger("slagalpha.cli").info(
            "archive ingestion complete",
            extra={
                "event": "archive_ingestion_complete",
                "symbol": spec.symbol,
                "interval": spec.interval,
                "period": spec.period,
                "rows": ingestion_result.normalization.normalized_row_count,
            },
        )
        print(
            json.dumps(
                ingestion_result.public_dict(), ensure_ascii=False, sort_keys=True
            )
        )
        return 0

    if args.command == "archive-plan":
        try:
            settings = Settings.from_env()
            spec = ArchiveSpec(
                symbol=args.symbol,
                interval=args.interval,
                year=args.start.year,
                month=args.start.month,
            )
            prefix = f"{MONTHLY_KLINE_PREFIX}{spec.symbol}/{spec.interval}/"
            inventory = fetch_archive_inventory(prefix)
            plan = plan_monthly_archives(
                inventory,
                symbol=spec.symbol,
                interval=spec.interval,
                start=args.start,
                end_exclusive=args.end_exclusive,
            )
            inventory_path = write_archive_inventory_manifest(
                inventory,
                settings.data_dir / "manifests",
            )
            plan_path = write_archive_plan_manifest(
                plan,
                settings.data_dir / "manifests",
            )
        except (ValidationError, ArchiveInventoryError, OSError) as error:
            print(f"archive planning failed: {error}", file=sys.stderr)
            return 1

        print(
            json.dumps(
                {
                    "inventory_hash": inventory.listing_hash,
                    "inventory_manifest_path": str(inventory_path),
                    "plan_hash": plan.plan_hash,
                    "plan_manifest_path": str(plan_path),
                    "available_periods": [item.period for item in plan.available],
                    "missing_periods": list(plan.missing_periods),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "archive-capacity":
        try:
            settings = Settings.from_env()
            root_inventory = ArchiveInventory.model_validate_json(
                args.root_inventory_manifest.read_text(encoding="utf-8")
            )
            if root_inventory.prefix != MONTHLY_KLINE_PREFIX:
                raise ArchiveCapacityError(
                    "root inventory manifest does not describe the monthly-kline root"
                )
            threshold_bytes = int(
                (args.threshold_gib * Decimal(1024**3)).to_integral_value()
            )

            def progress(completed: int, total: int) -> None:
                if completed % 100 == 0 or completed == total:
                    print(
                        f"archive capacity inventory: {completed}/{total}",
                        file=sys.stderr,
                        flush=True,
                    )

            estimate, rows = estimate_archive_capacity(
                symbols=root_inventory.archived_symbols,
                start=args.start,
                end_exclusive=args.end_exclusive,
                root_listing_hash=root_inventory.listing_hash,
                exchange_info_hash=args.exchange_info_hash,
                max_workers=args.max_workers,
                projection_multiplier=args.projection_multiplier,
                threshold_bytes=threshold_bytes,
                progress=progress,
            )
            parquet_path, manifest_path = write_archive_capacity_artifacts(
                estimate,
                rows,
                settings.data_dir,
            )
        except (ValidationError, ArchiveCapacityError, OSError) as error:
            print(f"archive capacity planning failed: {error}", file=sys.stderr)
            return 1

        print(
            json.dumps(
                {
                    "plan_content_hash": estimate.plan_content_hash,
                    "manifest_path": str(manifest_path),
                    "parquet_path": str(parquet_path),
                    "complete": estimate.complete,
                    "symbol_count": estimate.symbol_count,
                    "requested_prefix_count": estimate.requested_prefix_count,
                    "failed_prefix_count": len(estimate.failed_prefixes),
                    "zip_object_count": estimate.zip_object_count,
                    "checksum_missing_count": estimate.checksum_missing_count,
                    "downloadable_zip_bytes": estimate.downloadable_zip_bytes,
                    "projected_total_bytes": estimate.projected_total_bytes,
                    "threshold_bytes": estimate.threshold_bytes,
                    "within_threshold": estimate.within_threshold,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "archive-download-plan":
        try:
            settings = Settings.from_env()
            capacity_payload = json.loads(
                args.capacity_manifest.read_text(encoding="utf-8")
            )
            if not isinstance(capacity_payload, dict):
                raise ArchiveBatchError("capacity manifest root must be an object")
            estimate = ArchiveCapacityEstimate.model_validate(
                capacity_payload.get("estimate")
            )
            if not estimate.complete or not estimate.within_threshold:
                raise ArchiveBatchError(
                    "capacity plan is incomplete or exceeds its accepted threshold"
                )
            parquet_path = (
                settings.data_dir
                / "normalized"
                / "archive_capacity"
                / f"{estimate.plan_content_hash}.parquet"
            )
            expected_parquet_sha256 = capacity_payload.get("parquet_sha256")
            if not isinstance(expected_parquet_sha256, str) or (
                sha256_file(parquet_path) != expected_parquet_sha256
            ):
                raise ArchiveBatchError("capacity Parquet hash does not match manifest")
            rows = pd.read_parquet(parquet_path)

            def progress(completed: int, total: int) -> None:
                if completed % 500 == 0 or completed == total:
                    print(
                        f"archive downloads verified: {completed}/{total}",
                        file=sys.stderr,
                        flush=True,
                    )

            batch_result = download_capacity_plan(
                rows,
                plan_content_hash=estimate.plan_content_hash,
                raw_data_dir=settings.data_dir / "raw",
                manifests_dir=settings.data_dir / "manifests",
                max_workers=args.max_workers,
                progress=progress,
            )
            result_path = write_archive_batch_result(
                batch_result,
                settings.data_dir / "manifests",
            )
        except (
            ValidationError,
            ArchiveBatchError,
            OSError,
            ValueError,
        ) as error:
            print(f"archive batch download failed: {error}", file=sys.stderr)
            return 1

        print(
            json.dumps(
                {
                    "result_hash": batch_result.result_hash,
                    "result_path": str(result_path),
                    "complete": batch_result.complete,
                    "requested_count": batch_result.requested_count,
                    "downloaded_count": batch_result.downloaded_count,
                    "reused_count": batch_result.reused_count,
                    "failed_count": batch_result.failed_count,
                    "verified_bytes": batch_result.verified_bytes,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if batch_result.complete else 1

    if args.command == "archive-normalize-plan":
        try:
            settings = Settings.from_env()
            capacity_payload = json.loads(
                args.capacity_manifest.read_text(encoding="utf-8")
            )
            if not isinstance(capacity_payload, dict):
                raise NormalizationBatchError("capacity manifest root must be an object")
            estimate = ArchiveCapacityEstimate.model_validate(
                capacity_payload.get("estimate")
            )
            if not estimate.complete or not estimate.within_threshold:
                raise NormalizationBatchError(
                    "capacity plan is incomplete or exceeds its accepted threshold"
                )
            parquet_path = (
                settings.data_dir
                / "normalized"
                / "archive_capacity"
                / f"{estimate.plan_content_hash}.parquet"
            )
            expected_parquet_sha256 = capacity_payload.get("parquet_sha256")
            if not isinstance(expected_parquet_sha256, str) or (
                sha256_file(parquet_path) != expected_parquet_sha256
            ):
                raise NormalizationBatchError(
                    "capacity Parquet hash does not match manifest"
                )
            rows = pd.read_parquet(parquet_path)
            evaluated_at = datetime.fromisoformat(
                f"{estimate.end_period_exclusive}-01T00:00:00+00:00"
            ).astimezone(UTC)

            def progress(completed: int, total: int) -> None:
                if completed % 500 == 0 or completed == total:
                    print(
                        f"archive normalizations verified: {completed}/{total}",
                        file=sys.stderr,
                        flush=True,
                    )

            normalization_result = normalize_capacity_plan(
                rows,
                plan_content_hash=estimate.plan_content_hash,
                raw_data_dir=settings.data_dir / "raw",
                normalized_data_dir=settings.data_dir / "normalized",
                manifests_dir=settings.data_dir / "manifests",
                evaluated_at=evaluated_at,
                max_workers=args.max_workers,
                progress=progress,
            )
            result_path = write_normalization_batch_result(
                normalization_result,
                settings.data_dir / "manifests",
            )
        except (
            ValidationError,
            NormalizationBatchError,
            OSError,
            ValueError,
        ) as error:
            print(f"archive batch normalization failed: {error}", file=sys.stderr)
            return 1

        print(
            json.dumps(
                {
                    "result_hash": normalization_result.result_hash,
                    "result_path": str(result_path),
                    "dataset_content_hash": normalization_result.dataset_content_hash,
                    "complete": normalization_result.complete,
                    "requested_count": normalization_result.requested_count,
                    "normalized_count": normalization_result.normalized_count,
                    "reused_count": normalization_result.reused_count,
                    "failed_count": normalization_result.failed_count,
                    "normalized_row_count": normalization_result.normalized_row_count,
                    "normalized_parquet_bytes": (
                        normalization_result.normalized_parquet_bytes
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if normalization_result.complete else 1

    parser.error(f"unknown command: {args.command}")
