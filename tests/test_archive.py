"""Tests for Binance public archive download and checksum handling."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from slagalpha.data.archive import (
    ArchiveSpec,
    ChecksumFormatError,
    ChecksumMismatchError,
    ExistingArchiveMismatchError,
    archive_path,
    fetch_archive,
    parse_checksum,
    write_download_manifest,
)


def test_archive_spec_builds_fixed_official_url() -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()

    assert spec.filename == "BTCUSDT-15m-2024-01.zip"
    assert spec.url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "BTCUSDT/15m/BTCUSDT-15m-2024-01.zip"
    )
    assert spec.checksum_url == f"{spec.url}.CHECKSUM"


def test_archive_spec_normalizes_symbol_and_rejects_path_characters() -> None:
    assert ArchiveSpec(symbol="btcusdt", interval="15m", year=2024, month=1).symbol == (
        "BTCUSDT"
    )
    with pytest.raises(ValueError, match="symbol"):
        ArchiveSpec(symbol="../BTCUSDT", interval="15m", year=2024, month=1)


def test_archive_spec_accepts_official_unicode_symbol_without_weakening_paths() -> None:
    spec = ArchiveSpec(symbol="币安人生usdt", interval="15m", year=2026, month=7)

    assert spec.symbol == "币安人生USDT"
    assert "币安人生USDT" in spec.url
    with pytest.raises(ValueError, match="symbol"):
        ArchiveSpec(symbol="币安/人生USDT", interval="15m", year=2026, month=7)


def test_parse_checksum_requires_matching_filename() -> None:
    digest = "a" * 64
    assert (
        parse_checksum(
            f"{digest}  BTCUSDT-15m-2024-01.zip\n",
            "BTCUSDT-15m-2024-01.zip",
        )
        == digest
    )

    with pytest.raises(ChecksumFormatError, match="does not match"):
        parse_checksum(f"{digest}  ETHUSDT-15m-2024-01.zip", "BTCUSDT-15m-2024-01.zip")


def test_fetch_archive_verifies_and_reuses_existing_bytes(tmp_path: Path) -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    body = b"verified archive bytes"
    digest = hashlib.sha256(body).hexdigest()
    request_counts = {"checksum": 0, "archive": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == spec.checksum_url:
            request_counts["checksum"] += 1
            return httpx.Response(200, text=f"{digest}  {spec.filename}\n")
        if str(request.url) == spec.url:
            request_counts["archive"] += 1
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        destination, manifest = fetch_archive(
            spec, tmp_path / "raw", client=client, sleep=lambda _: None
        )
        second_destination, second_manifest = fetch_archive(
            spec, tmp_path / "raw", client=client, sleep=lambda _: None
        )

    assert destination == second_destination
    assert destination.read_bytes() == body
    assert manifest.actual_sha256 == digest
    assert second_manifest.actual_sha256 == digest
    assert request_counts == {"checksum": 2, "archive": 1}


def test_fetch_archive_bad_checksum_leaves_no_final_or_part_file(tmp_path: Path) -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    official_digest = hashlib.sha256(b"expected").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == spec.checksum_url:
            return httpx.Response(200, text=f"{official_digest}  {spec.filename}")
        return httpx.Response(200, content=b"corrupt")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChecksumMismatchError):
            fetch_archive(spec, tmp_path / "raw", client=client, sleep=lambda _: None)

    destination = archive_path(tmp_path / "raw", spec)
    assert not destination.exists()
    assert list(destination.parent.glob("*.part")) == []


def test_existing_mismatch_is_preserved(tmp_path: Path) -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    destination = archive_path(tmp_path / "raw", spec)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"local old version")
    official_digest = hashlib.sha256(b"new official version").hexdigest()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"{official_digest}  {spec.filename}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExistingArchiveMismatchError, match="preserved"):
            fetch_archive(spec, tmp_path / "raw", client=client, sleep=lambda _: None)

    assert destination.read_bytes() == b"local old version"


def test_download_manifest_is_content_addressed(tmp_path: Path) -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    body = b"archive"
    digest = hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == spec.checksum_url:
            return httpx.Response(200, text=f"{digest}  {spec.filename}")
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, manifest = fetch_archive(spec, tmp_path / "raw", client=client)

    manifest_path = write_download_manifest(manifest, tmp_path / "manifests")
    assert manifest_path.name == f"{digest}.json"
    assert write_download_manifest(manifest, tmp_path / "manifests") == manifest_path
