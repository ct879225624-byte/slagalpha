"""Narrow, content-addressed verification of historical Funding mark prices."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.download_cache import (
    DownloadProvenance,
    build_download_task,
    commit_staged_download,
)
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.replay_inputs import Sha256

BASE_URL: Literal["https://fapi.binance.com"] = "https://fapi.binance.com"
PROVIDER: Literal["BINANCE_USDM"] = "BINANCE_USDM"
FUNDING_ENDPOINT: Literal["/fapi/v1/fundingRate"] = "/fapi/v1/fundingRate"
MARK_PRICE_ENDPOINT: Literal["/fapi/v1/markPriceKlines"] = "/fapi/v1/markPriceKlines"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class FundingMarkSupplementError(ValueError):
    """A mark-price verification or supplement failed closed."""


def _hash(payload: Any) -> str:
    normalized = json.loads(json.dumps(payload, default=str))
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise FundingMarkSupplementError("mark-price values must be nonempty decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise FundingMarkSupplementError("mark-price value is not decimal") from error
    if not result.is_finite() or result <= 0:
        raise FundingMarkSupplementError("mark-price value must be finite and positive")
    return result


def _path(value: str) -> str:
    parsed = PurePosixPath(value)
    if (
        not parsed.parts
        or parsed.is_absolute()
        or str(parsed) != value
        or "\\" in value
        or any(part in {".", ".."} or ":" in part for part in parsed.parts)
    ):
        raise FundingMarkSupplementError("raw response path is unsafe")
    return value


def _url(endpoint: str, parameters: tuple[tuple[str, str], ...]) -> str:
    return str(httpx.URL(f"{BASE_URL}{endpoint}", params=dict(parameters)))


def _funding_parameters(symbol: str, funding_ms: int) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                ("endTime", str(funding_ms)),
                ("limit", "1"),
                ("startTime", str(funding_ms)),
                ("symbol", symbol),
            )
        )
    )


def _mark_parameters(symbol: str, funding_ms: int) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                ("endTime", str(funding_ms + 59999)),
                ("interval", "1m"),
                ("limit", "1"),
                ("startTime", str(funding_ms)),
                ("symbol", symbol),
            )
        )
    )


class FundingMarkSupplementArtifact(BaseModel):
    """One verified Funding mark resolution; original Funding data is immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["funding-mark-supplement/0.1.0"] = "funding-mark-supplement/0.1.0"
    request_hash: Sha256
    funding_artifact_hash: Sha256
    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    original_funding_mark_price: None = None
    funding_history_endpoint: Literal["/fapi/v1/fundingRate"] = FUNDING_ENDPOINT
    funding_history_parameters: tuple[tuple[str, str], ...]
    funding_history_url: str
    funding_history_response_sha256: Sha256
    funding_history_relative_path: str
    funding_history_observed_at: datetime
    verification_status: Literal["NATIVE", "DEV_APPROXIMATE"]
    native_mark_price: Decimal | None = None
    mark_price_endpoint: Literal["/fapi/v1/markPriceKlines"] | None = None
    mark_price_parameters: tuple[tuple[str, str], ...] | None = None
    mark_price_url: str | None = None
    mark_price_open: Decimal | None = None
    mark_price_high: Decimal | None = None
    mark_price_low: Decimal | None = None
    mark_price_close: Decimal | None = None
    selected_proxy: Decimal | None = None
    selection_rule: str
    mark_price_response_sha256: Sha256 | None = None
    mark_price_relative_path: str | None = None
    warning: Literal["DEV_APPROXIMATE_FUNDING_MARK"] | None = None
    artifact_hash: Sha256

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip().upper():
            raise ValueError("supplement symbol must be uppercase")
        return value

    @field_validator("funding_time", "funding_history_observed_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("supplement timestamps must use UTC")
        return value

    @field_validator("funding_history_relative_path")
    @classmethod
    def validate_history_path(cls, value: str) -> str:
        return _path(value)

    @field_validator("mark_price_relative_path")
    @classmethod
    def validate_mark_path(cls, value: str | None) -> str | None:
        return _path(value) if value is not None else None

    @field_validator("funding_history_parameters", "mark_price_parameters")
    @classmethod
    def validate_parameters(
        cls, value: tuple[tuple[str, str], ...] | None
    ) -> tuple[tuple[str, str], ...] | None:
        if value is not None and value != tuple(sorted(set(value))):
            raise ValueError("supplement query parameters must be canonical")
        return value

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        funding_ms = int(self.funding_time.timestamp() * 1000)
        if self.funding_history_parameters != _funding_parameters(self.symbol, funding_ms):
            raise ValueError("Funding recheck must be the exact one-event query")
        if self.funding_history_url != _url(FUNDING_ENDPOINT, self.funding_history_parameters):
            raise ValueError("Funding recheck URL does not match parameters")
        if self.verification_status == "NATIVE":
            if (
                self.native_mark_price is None
                or self.mark_price_endpoint is not None
                or self.mark_price_parameters is not None
                or self.mark_price_response_sha256 is not None
                or self.warning is not None
            ):
                raise ValueError("native verification cannot carry an approximate supplement")
        else:
            expected = _mark_parameters(self.symbol, funding_ms)
            if (
                self.native_mark_price is not None
                or self.mark_price_endpoint != MARK_PRICE_ENDPOINT
                or self.mark_price_parameters != expected
                or self.mark_price_url != _url(MARK_PRICE_ENDPOINT, expected)
                or any(
                    value is None
                    for value in (
                        self.mark_price_open,
                        self.mark_price_high,
                        self.mark_price_low,
                        self.mark_price_close,
                        self.selected_proxy,
                        self.mark_price_response_sha256,
                        self.mark_price_relative_path,
                    )
                )
                or self.selected_proxy != self.mark_price_open
                or self.selection_rule != "OPEN_OF_EXACT_1M_MARK_PRICE_CANDLE"
                or self.warning != "DEV_APPROXIMATE_FUNDING_MARK"
            ):
                raise ValueError("approximate supplement is incomplete or not deterministic")
        payload = self.model_dump(mode="json", exclude={"artifact_hash"})
        if self.artifact_hash != _hash(payload):
            raise ValueError("Funding mark supplement hash mismatch")
        return self


class FundingMarkSupplementRun(BaseModel):
    """Immutable index of the five event verifications."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["funding-mark-supplement-run/0.1.0"] = (
        "funding-mark-supplement-run/0.1.0"
    )
    requirements_plan_hash: Sha256
    transport_plan_hash: Sha256
    source_report_hash: Sha256
    artifacts: tuple[Sha256, ...]
    raw_body_bytes: int = Field(ge=0, strict=True)
    artifact_hash: Sha256

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if self.artifacts != tuple(sorted(set(self.artifacts))):
            raise ValueError("supplement artifacts must be canonical")
        payload = self.model_dump(mode="json", exclude={"artifact_hash"})
        if self.artifact_hash != _hash(payload):
            raise ValueError("Funding supplement run hash mismatch")
        return self


class FundingMarkUncertainty(BaseModel):
    """Full-open DEV sensitivity; realized P7 cash flow scales by open fraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_hash: Sha256
    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    direction: Literal["LONG", "SHORT"]
    risk_usdt_per_unit: Decimal
    open_proxy_fee: Decimal
    low_fee: Decimal
    high_fee: Decimal
    fee_range_low: Decimal
    fee_range_high: Decimal
    max_absolute_error_usdt: Decimal
    max_error_r: Decimal
    open_fraction_assumption: Literal["FULL_OPEN_1.0"] = "FULL_OPEN_1.0"


class FundingMarkUncertaintyReport(BaseModel):
    """Persisted quantitative check for all resolved Funding mark events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["funding-mark-uncertainty/0.1.0"] = "funding-mark-uncertainty/0.1.0"
    supplement_artifact_hashes: tuple[Sha256, ...]
    items: tuple[FundingMarkUncertainty, ...]
    report_hash: Sha256

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        # One replay request can cross more than one Funding settlement when a
        # longer holding horizon is used.  Uncertainty rows are therefore
        # unique by request *and* settlement time, not by request alone.
        keys = tuple((item.request_hash, item.funding_time) for item in self.items)
        if self.supplement_artifact_hashes != tuple(sorted(set(self.supplement_artifact_hashes))):
            raise ValueError("uncertainty supplement hashes must be canonical")
        if len(keys) != len(set(keys)):
            raise ValueError("uncertainty items must be unique")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        if self.report_hash != _hash(payload):
            raise ValueError("Funding uncertainty report hash mismatch")
        return self


def quantify_funding_mark_uncertainty(
    *, request: Any, artifact: FundingMarkSupplementArtifact
) -> FundingMarkUncertainty:
    """Quantify mark OHLC uncertainty without running Replay or changing P7."""

    if artifact.verification_status == "NATIVE":
        open_mark = low_mark = high_mark = artifact.native_mark_price
    else:
        open_mark = artifact.selected_proxy
        low_mark = artifact.mark_price_low
        high_mark = artifact.mark_price_high
    if open_mark is None or low_mark is None or high_mark is None:
        raise FundingMarkSupplementError("cannot quantify an incomplete mark supplement")
    direction = request.request.armed.direction.value
    if direction not in {"LONG", "SHORT"}:
        raise FundingMarkSupplementError("request direction is unsupported")
    sign = Decimal("-1") if direction == "LONG" else Decimal("1")
    open_fee = sign * open_mark * artifact.funding_rate
    low_fee = sign * low_mark * artifact.funding_rate
    high_fee = sign * high_mark * artifact.funding_rate
    risk = abs(request.request.armed.entry_price - request.request.armed.stop_price)
    if risk <= 0:
        raise FundingMarkSupplementError("request risk must be positive")
    error = max(abs(low_fee - open_fee), abs(high_fee - open_fee))
    return FundingMarkUncertainty(
        request_hash=request.request_hash,
        symbol=artifact.symbol,
        funding_time=artifact.funding_time,
        funding_rate=artifact.funding_rate,
        direction=direction,
        risk_usdt_per_unit=risk,
        open_proxy_fee=open_fee,
        low_fee=low_fee,
        high_fee=high_fee,
        fee_range_low=min(low_fee, high_fee),
        fee_range_high=max(low_fee, high_fee),
        max_absolute_error_usdt=error,
        max_error_r=error / risk,
    )


def write_funding_mark_supplement_artifact(
    artifact: FundingMarkSupplementArtifact, data_dir: Path
) -> Path:
    artifact = FundingMarkSupplementArtifact.model_validate(artifact.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "funding_mark_supplement" / f"{artifact.artifact_hash}.json"
    )
    content = canonical_json_bytes(artifact.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise FundingMarkSupplementError("existing supplement artifact changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        _publish_immutable(destination, content)
    return destination


def write_funding_mark_supplement_run(run: FundingMarkSupplementRun, data_dir: Path) -> Path:
    run = FundingMarkSupplementRun.model_validate(run.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "funding_mark_supplement_run" / f"{run.artifact_hash}.json"
    )
    content = canonical_json_bytes(run.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise FundingMarkSupplementError("existing supplement run changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        _publish_immutable(destination, content)
    return destination


def write_funding_mark_uncertainty_report(
    report: FundingMarkUncertaintyReport, data_dir: Path
) -> Path:
    report = FundingMarkUncertaintyReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests" / "funding_mark_uncertainty" / f"{report.report_hash}.json"
    content = canonical_json_bytes(report.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise FundingMarkSupplementError("existing uncertainty report changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        _publish_immutable(destination, content)
    return destination


def _read_raw(project_dir: Path, relative_path: str, expected_hash: str) -> bytes:
    candidate = project_dir / PurePosixPath(relative_path)
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or not candidate.resolve().is_relative_to(project_dir.resolve())
    ):
        raise FundingMarkSupplementError("supplement raw response is missing or outside project")
    content = candidate.read_bytes()
    if len(content) > MAX_RESPONSE_BYTES or hashlib.sha256(content).hexdigest() != expected_hash:
        raise FundingMarkSupplementError("supplement raw response hash or size mismatch")
    return content


def _json_array(content: bytes) -> list[Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FundingMarkSupplementError("supplement raw response is not JSON") from error
    if not isinstance(value, list):
        raise FundingMarkSupplementError("supplement response must be a JSON array")
    return value


def verify_funding_mark_supplement(
    *, project_dir: Path, artifact: FundingMarkSupplementArtifact
) -> Decimal:
    """Validate both raw responses and return the mark value approved for DEV."""

    artifact = FundingMarkSupplementArtifact.model_validate(artifact.model_dump(mode="json"))
    funding_rows = _json_array(
        _read_raw(
            project_dir,
            artifact.funding_history_relative_path,
            artifact.funding_history_response_sha256,
        )
    )
    funding_ms = int(artifact.funding_time.timestamp() * 1000)
    if len(funding_rows) != 1 or not isinstance(funding_rows[0], dict):
        raise FundingMarkSupplementError("Funding recheck must return exactly one row")
    row = funding_rows[0]
    try:
        observed_rate = Decimal(str(row.get("fundingRate")))
    except (InvalidOperation, ValueError) as error:
        raise FundingMarkSupplementError("Funding recheck rate is not decimal") from error
    if (
        row.get("symbol") != artifact.symbol
        or row.get("fundingTime") != funding_ms
        or observed_rate != artifact.funding_rate
    ):
        raise FundingMarkSupplementError(
            "Funding recheck event or rate does not match original artifact"
        )
    mark = row.get("markPrice")
    if artifact.original_funding_mark_price is not None:
        raise FundingMarkSupplementError("original Funding markPrice must remain null")
    if artifact.verification_status == "NATIVE":
        if mark in (None, "") or _decimal(mark) != artifact.native_mark_price:
            raise FundingMarkSupplementError("native Funding markPrice does not match artifact")
        return artifact.native_mark_price
    if mark not in (None, ""):
        raise FundingMarkSupplementError(
            "approximate supplement is not allowed when native markPrice is present"
        )
    mark_rows = _json_array(
        _read_raw(
            project_dir,
            artifact.mark_price_relative_path or "",
            artifact.mark_price_response_sha256 or "",
        )
    )
    if len(mark_rows) != 1 or not isinstance(mark_rows[0], list) or len(mark_rows[0]) != 12:
        raise FundingMarkSupplementError("Mark Price Kline must return exactly one complete candle")
    candle = mark_rows[0]
    if (
        type(candle[0]) is not int
        or type(candle[6]) is not int
        or candle[0] != funding_ms
        or candle[6] != funding_ms + 59999
    ):
        raise FundingMarkSupplementError("Mark Price Kline does not exactly contain fundingTime")
    values = tuple(_decimal(candle[index]) for index in (1, 2, 3, 4))
    if values != (
        artifact.mark_price_open,
        artifact.mark_price_high,
        artifact.mark_price_low,
        artifact.mark_price_close,
    ):
        raise FundingMarkSupplementError("Mark Price Kline OHLC does not match supplement")
    return artifact.selected_proxy or Decimal(0)


def _write_staged(path: Path, content: bytes) -> None:
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


def _request(
    *,
    client: httpx.Client,
    endpoint: str,
    parameters: tuple[tuple[str, str], ...],
    max_attempts: int,
    sleep: Callable[[float], None],
) -> tuple[bytes, httpx.Response, int]:
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.get(f"{BASE_URL}{endpoint}", params=dict(parameters))
            if response.status_code == 200 and len(response.content) <= MAX_RESPONSE_BYTES:
                _json_array(response.content)
                return response.content, response, attempt
            error = (
                f"HTTP {response.status_code}"
                if response.status_code != 200
                else "response exceeds size limit"
            )
            retryable = response.status_code in RETRYABLE_STATUSES
        except httpx.TransportError:
            response = None
            retryable = True
        if not retryable or attempt == max_attempts:
            raise FundingMarkSupplementError(
                f"{endpoint} failed after {attempt} attempt(s): {error}"
            )
        sleep(min(2 ** (attempt - 1), 8))
    raise AssertionError("unreachable")


def fetch_funding_mark_event(
    *,
    client: httpx.Client,
    project_dir: Path,
    data_dir: Path,
    request_hash: str,
    funding_artifact_hash: str,
    symbol: str,
    funding_time: datetime,
    funding_rate: Decimal,
    max_attempts: int = 3,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> FundingMarkSupplementArtifact:
    """Fetch only one Funding event and, when needed, its exact mark-price minute."""

    funding_ms = int(funding_time.timestamp() * 1000)
    history_parameters = _funding_parameters(symbol, funding_ms)
    history_content, history_response, _ = _request(
        client=client,
        endpoint=FUNDING_ENDPOINT,
        parameters=history_parameters,
        max_attempts=max_attempts,
        sleep=sleep,
    )
    observed_at = now()
    if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
        raise FundingMarkSupplementError("supplement clock must return UTC")
    cache_dir = data_dir / "cache"
    staging_dir = data_dir / "funding_mark_supplement" / "staging"
    history_staged = staging_dir / f"{request_hash}-{funding_ms}-funding.part"
    _write_staged(history_staged, history_content)
    history_task = build_download_task(
        provider=PROVIDER,
        endpoint=FUNDING_ENDPOINT,
        parameters=history_parameters,
        max_attempts=max_attempts,
    )
    history_commit = commit_staged_download(
        staged_path=history_staged,
        cache_dir=cache_dir,
        provenance=DownloadProvenance(
            cache_key=history_task.cache_key,
            provider=PROVIDER,
            endpoint=FUNDING_ENDPOINT,
            parameters=history_parameters,
            observed_at=observed_at,
            http_status=200,
            etag=history_response.headers.get("etag"),
            last_modified=history_response.headers.get("last-modified"),
        ),
    )
    history_staged.unlink(missing_ok=True)
    history_blob = (
        (cache_dir / PurePosixPath(history_commit.relative_path))
        .resolve()
        .relative_to(project_dir.resolve())
        .as_posix()
    )
    rows = _json_array(history_content)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise FundingMarkSupplementError("Funding recheck must return exactly one row")
    row = rows[0]
    if (
        row.get("symbol") != symbol
        or row.get("fundingTime") != funding_ms
        or row.get("rateType", "Regular") != "Regular"
    ):
        raise FundingMarkSupplementError("Funding recheck returned the wrong event")
    try:
        observed_rate = Decimal(str(row.get("fundingRate")))
    except (InvalidOperation, ValueError) as error:
        raise FundingMarkSupplementError("Funding recheck rate is not decimal") from error
    if observed_rate != funding_rate:
        raise FundingMarkSupplementError("Funding recheck rate differs from original artifact")
    native = None if row.get("markPrice") in (None, "") else _decimal(row.get("markPrice"))
    common: dict[str, Any] = {
        "schema_version": "funding-mark-supplement/0.1.0",
        "request_hash": request_hash,
        "funding_artifact_hash": funding_artifact_hash,
        "symbol": symbol,
        "funding_time": funding_time.isoformat().replace("+00:00", "Z"),
        "funding_rate": funding_rate,
        "original_funding_mark_price": None,
        "funding_history_endpoint": FUNDING_ENDPOINT,
        "funding_history_parameters": history_parameters,
        "funding_history_url": str(history_response.request.url),
        "funding_history_response_sha256": history_commit.content_sha256,
        "funding_history_relative_path": history_blob,
        "funding_history_observed_at": observed_at.isoformat().replace("+00:00", "Z"),
    }
    if native is not None:
        payload = {
            **common,
            "verification_status": "NATIVE",
            "native_mark_price": native,
            "selection_rule": "NATIVE_BINANCE_FUNDING_MARK_PRICE",
            "mark_price_endpoint": None,
            "mark_price_parameters": None,
            "mark_price_url": None,
            "mark_price_open": None,
            "mark_price_high": None,
            "mark_price_low": None,
            "mark_price_close": None,
            "selected_proxy": None,
            "mark_price_response_sha256": None,
            "mark_price_relative_path": None,
            "warning": None,
        }
        return FundingMarkSupplementArtifact.model_validate(
            {**payload, "artifact_hash": _hash(payload)}
        )

    mark_parameters = _mark_parameters(symbol, funding_ms)
    mark_content, mark_response, _ = _request(
        client=client,
        endpoint=MARK_PRICE_ENDPOINT,
        parameters=mark_parameters,
        max_attempts=max_attempts,
        sleep=sleep,
    )
    mark_staged = staging_dir / f"{request_hash}-{funding_ms}-mark.part"
    _write_staged(mark_staged, mark_content)
    mark_task = build_download_task(
        provider=PROVIDER,
        endpoint=MARK_PRICE_ENDPOINT,
        parameters=mark_parameters,
        max_attempts=max_attempts,
    )
    mark_commit = commit_staged_download(
        staged_path=mark_staged,
        cache_dir=cache_dir,
        provenance=DownloadProvenance(
            cache_key=mark_task.cache_key,
            provider=PROVIDER,
            endpoint=MARK_PRICE_ENDPOINT,
            parameters=mark_parameters,
            observed_at=observed_at,
            http_status=200,
            etag=mark_response.headers.get("etag"),
            last_modified=mark_response.headers.get("last-modified"),
        ),
    )
    mark_staged.unlink(missing_ok=True)
    mark_blob = (
        (cache_dir / PurePosixPath(mark_commit.relative_path))
        .resolve()
        .relative_to(project_dir.resolve())
        .as_posix()
    )
    mark_rows = _json_array(mark_content)
    if len(mark_rows) != 1 or not isinstance(mark_rows[0], list) or len(mark_rows[0]) != 12:
        raise FundingMarkSupplementError("Mark Price Kline must return exactly one candle")
    candle = mark_rows[0]
    if (
        type(candle[0]) is not int
        or type(candle[6]) is not int
        or candle[0] != funding_ms
        or candle[6] != funding_ms + 59999
    ):
        raise FundingMarkSupplementError("Mark Price Kline time range is not exact")
    o, h, low, c = (_decimal(candle[index]) for index in (1, 2, 3, 4))
    payload = {
        **common,
        "verification_status": "DEV_APPROXIMATE",
        "native_mark_price": None,
        "mark_price_endpoint": MARK_PRICE_ENDPOINT,
        "mark_price_parameters": mark_parameters,
        "mark_price_url": str(mark_response.request.url),
        "mark_price_open": o,
        "mark_price_high": h,
        "mark_price_low": low,
        "mark_price_close": c,
        "selected_proxy": o,
        "selection_rule": "OPEN_OF_EXACT_1M_MARK_PRICE_CANDLE",
        "mark_price_response_sha256": mark_commit.content_sha256,
        "mark_price_relative_path": mark_blob,
        "warning": "DEV_APPROXIMATE_FUNDING_MARK",
    }
    return FundingMarkSupplementArtifact.model_validate(
        {**payload, "artifact_hash": _hash(payload)}
    )
