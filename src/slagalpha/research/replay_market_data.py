"""Offline validation of bounded, saved public REST responses for one DEV request."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.backtest.analytics import FundingDataset, FundingObservation
from slagalpha.backtest.replay import validate_replay_candles
from slagalpha.data.archive import ArchiveSpec
from slagalpha.data.klines import RAW_COLUMNS, _normalized_content_hash, normalize_klines
from slagalpha.domain.symbols import normalize_symbol
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.replay_inputs import DevReplayDataRequest, ReplayDataInputError, Sha256

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class ReplayRestResponse(BaseModel):
    """A saved public response and its exact query; this is not an authenticated receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: Literal["https://fapi.binance.com"] = "https://fapi.binance.com"
    endpoint: Literal["/fapi/v1/klines", "/fapi/v1/fundingRate"]
    symbol: str
    interval: Literal["1m"] | None = None
    start_time_ms: int = Field(ge=0, strict=True)
    end_time_ms: int = Field(ge=0, strict=True)
    limit: int = Field(gt=0, strict=True)
    observed_at: datetime
    http_status: Literal[200] = 200
    relative_path: str
    body_sha256: Sha256

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("observed_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("REST observation time must use UTC")
        return value

    @field_validator("relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (not path.parts or path.is_absolute() or str(path) != value or "\\" in value
            or any(part == ".." or ":" in part for part in path.parts)):
            raise ValueError("response path must be canonical and project-relative")
        return value

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if self.end_time_ms < self.start_time_ms:
            raise ValueError("REST query end must not precede start")
        kline = self.endpoint == "/fapi/v1/klines"
        if (kline and self.interval != "1m") or (not kline and self.interval is not None):
            raise ValueError("REST interval does not match endpoint")
        if self.limit > (1500 if kline else 1000):
            raise ValueError("REST limit exceeds documented maximum")
        return self


class ReplayMarketDataArtifact(BaseModel):
    """One finite request's input artifact; not aggregate DEV coverage or execution permission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["replay-market-data/0.1.0"] = "replay-market-data/0.1.0"
    role: Literal["CANDLE_ONE_MINUTE", "FUNDING"]
    request_hash: Sha256
    responses: tuple[ReplayRestResponse, ...]
    record_count: int = Field(gt=0, strict=True)
    normalized_content_hash: Sha256
    research_authorized: Literal[False] = False
    artifact_hash: Sha256

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if not self.responses:
            raise ValueError("market-data artifact requires saved response evidence")
        endpoint = "/fapi/v1/klines" if self.role == "CANDLE_ONE_MINUTE" else "/fapi/v1/fundingRate"
        if any(item.endpoint != endpoint for item in self.responses):
            raise ValueError("market-data role and response endpoints disagree")
        payload = self.model_dump(mode="json", exclude={"artifact_hash"})
        if hashlib.sha256(canonical_json_bytes(payload)).hexdigest() != self.artifact_hash:
            raise ValueError("market-data artifact content hash mismatch")
        return self


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ReplayDataInputError("REST response contains duplicate JSON keys")
        result[key] = value
    return result


def _read_response(root: Path, response: ReplayRestResponse) -> list[Any]:
    candidate = root
    if root.is_symlink():
        raise ReplayDataInputError("response root cannot be a symlink")
    for part in PurePosixPath(response.relative_path).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ReplayDataInputError("response path cannot contain a symlink")
    if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
        raise ReplayDataInputError("response file missing or outside project")
    with candidate.open("rb") as stream:
        content = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ReplayDataInputError("bounded REST response exceeds size limit")
    if hashlib.sha256(content).hexdigest() != response.body_sha256:
        raise ReplayDataInputError("REST response file hash mismatch")
    payload = json.loads(content, object_pairs_hook=_unique_object)
    if not isinstance(payload, list) or len(payload) > response.limit:
        raise ReplayDataInputError("REST response must be a bounded array, not an API error")
    return payload


def _decimal_string(value: Any) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ReplayDataInputError("REST financial values must be nonempty decimal strings")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ReplayDataInputError("invalid REST decimal value") from error
    if not parsed.is_finite():
        raise ReplayDataInputError("REST decimal value must be finite")
    return parsed


def _optional_decimal_string(value: Any) -> Decimal | None:
    if value == "":
        return None
    return _decimal_string(value)


def _query_rows(
    request: DevReplayDataRequest, responses: tuple[ReplayRestResponse, ...], root: Path,
) -> list[tuple[ReplayRestResponse, list[Any]]]:
    if not responses:
        raise ReplayDataInputError("at least one saved REST response is required")
    cursor = int(request.start.timestamp() * 1000)
    end_ms = int(request.end_exclusive.timestamp() * 1000) - 1
    rows = []
    for response in responses:
        if (response.start_time_ms != cursor or response.end_time_ms > end_ms
            or response.symbol != request.request.armed.symbol
            or response.observed_at < request.end_exclusive):
            raise ReplayDataInputError(
                "REST queries must exactly partition the closed requested history"
            )
        rows.append((response, _read_response(root, response)))
        cursor = response.end_time_ms + 1
    if cursor != end_ms + 1:
        raise ReplayDataInputError("REST query coverage is incomplete")
    return rows


def _candles(
    request: DevReplayDataRequest, pages: list[tuple[ReplayRestResponse, list[Any]]],
) -> pd.DataFrame:
    frames = []
    for response, rows in pages:
        if (response.endpoint != "/fapi/v1/klines" or response.start_time_ms % 60000
            or (response.end_time_ms + 1) % 60000):
            raise ReplayDataInputError("1m queries must be complete minute-aligned intervals")
        expected = (response.end_time_ms + 1 - response.start_time_ms) // 60000
        if len(rows) != expected or not rows:
            raise ReplayDataInputError("1m response does not contain every requested minute")
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 12:
                raise ReplayDataInputError("1m response row must have 12 fields")
            if any(type(row[position]) is not int for position in (0, 6, 8)):
                raise ReplayDataInputError("1m times and trade count must be integers")
            if row[0] != response.start_time_ms + index * 60000:
                raise ReplayDataInputError(
                    "1m response grid is missing, duplicated, or out of order"
                )
            for position in (1, 2, 3, 4, 5, 7, 9, 10):
                _decimal_string(row[position])
        raw = pd.DataFrame(rows, columns=RAW_COLUMNS).astype(str)
        opens = pd.to_datetime(raw["open_time"].astype("int64"), unit="ms", utc=True)
        months = opens.dt.strftime("%Y-%m")
        for month in dict.fromkeys(months):
            year, month_number = (int(part) for part in month.split("-"))
            normalized, duplicates, _ = normalize_klines(
                raw.loc[months == month].reset_index(drop=True),
                ArchiveSpec(symbol=response.symbol, interval="1m", year=year, month=month_number),
                response.body_sha256, evaluated_at=response.observed_at,
            )
            if duplicates:
                raise ReplayDataInputError("1m duplicates must not be silently removed")
            normalized["source"] = "REST"
            frames.append(normalized)
    candles = pd.concat(frames, ignore_index=True)
    validate_replay_candles(candles)
    if len(candles) != request.expected_candle_count:
        raise ReplayDataInputError("1m record count differs from bounded request")
    return candles


def _funding(
    request: DevReplayDataRequest, pages: list[tuple[ReplayRestResponse, list[Any]]],
) -> FundingDataset:
    observations = []
    for response, rows in pages:
        if response.endpoint != "/fapi/v1/fundingRate" or len(rows) >= response.limit:
            raise ReplayDataInputError(
                "Funding response may be truncated; split the query interval"
            )
        last_time = response.start_time_ms - 1
        for row in rows:
            if not isinstance(row, dict) or set(row) - {
                "symbol", "fundingTime", "fundingRate", "markPrice", "rateType",
            }:
                raise ReplayDataInputError("unexpected Funding response shape")
            timestamp = row.get("fundingTime")
            if (row.get("symbol") != response.symbol or type(timestamp) is not int
                or not last_time < timestamp <= response.end_time_ms):
                raise ReplayDataInputError(
                    "Funding symbol/time is invalid, repeated, or out of order"
                )
            if row.get("rateType", "Regular") != "Regular":
                raise ReplayDataInputError("Special or unknown Funding rate type is unsupported")
            rate = _decimal_string(row.get("fundingRate"))
            # Binance historical Funding rows may publish an empty markPrice;
            # preserve that as missing so P7 can fail closed without zero-filling.
            mark = _optional_decimal_string(row.get("markPrice"))
            observations.append(FundingObservation(
                settlement_time=datetime.fromtimestamp(timestamp / 1000, UTC),
                rate=rate, mark_price=mark,
            ))
            last_time = timestamp
    if not observations:
        raise ReplayDataInputError("empty Funding history requires independent schedule evidence")
    return FundingDataset(
        coverage_start=request.start, coverage_end=request.end_exclusive,
        expected_settlement_times=tuple(item.settlement_time for item in observations),
        observations=tuple(observations),
    )


def build_replay_market_data_artifact(
    *, project_dir: Path, request: DevReplayDataRequest,
    role: Literal["CANDLE_ONE_MINUTE", "FUNDING"], responses: tuple[ReplayRestResponse, ...],
) -> tuple[ReplayMarketDataArtifact, pd.DataFrame | FundingDataset]:
    request = DevReplayDataRequest.model_validate(request.model_dump(mode="json"))
    responses = tuple(ReplayRestResponse.model_validate(item.model_dump(mode="json"))
                      for item in responses)
    pages = _query_rows(request, responses, project_dir)
    data: pd.DataFrame | FundingDataset
    if role == "CANDLE_ONE_MINUTE":
        data = _candles(request, pages)
        record_count = len(data)
        content_hash = _normalized_content_hash(data)
    elif role == "FUNDING":
        data = _funding(request, pages)
        record_count = len(data.observations)
        content_hash = hashlib.sha256(
            canonical_json_bytes(data.model_dump(mode="json"))
        ).hexdigest()
    else:
        raise ReplayDataInputError("unsupported replay data role")
    payload = {
        "schema_version": "replay-market-data/0.1.0", "role": role,
        "request_hash": request.request_hash,
        "responses": [item.model_dump(mode="json") for item in responses],
        "record_count": record_count, "normalized_content_hash": content_hash,
        "research_authorized": False,
    }
    artifact = ReplayMarketDataArtifact.model_validate({
        **payload, "artifact_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    return artifact, data


def load_replay_market_data_artifact(
    *, project_dir: Path, request: DevReplayDataRequest, artifact: ReplayMarketDataArtifact,
) -> pd.DataFrame | FundingDataset:
    artifact = ReplayMarketDataArtifact.model_validate(artifact.model_dump(mode="json"))
    if artifact.request_hash != request.request_hash:
        raise ReplayDataInputError("market-data artifact belongs to a different request")
    observed, data = build_replay_market_data_artifact(
        project_dir=project_dir, request=request, role=artifact.role, responses=artifact.responses,
    )
    if artifact != observed:
        raise ReplayDataInputError("market-data artifact no longer matches request or raw files")
    return data


def write_replay_market_data_artifact(artifact: ReplayMarketDataArtifact, data_dir: Path) -> Path:
    artifact = ReplayMarketDataArtifact.model_validate(artifact.model_dump(mode="json"))
    destination = data_dir / "manifests" / "replay_market_data" / f"{artifact.artifact_hash}.json"
    if not destination.resolve().is_relative_to(data_dir.resolve()):
        raise ReplayDataInputError("market-data artifact output escapes data directory")
    content = canonical_json_bytes(artifact.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise ReplayDataInputError("existing market-data artifact changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
