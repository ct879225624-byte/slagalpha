"""Saved REST bodies are explicit synthetic fixtures, never historical exchange evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pytest
from pydantic import ValidationError

from slagalpha.backtest.analytics import FundingDataset
from slagalpha.data.klines import CandleValidationError
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    ReplayDataInputError,
    build_dev_replay_data_request,
)
from slagalpha.research.replay_market_data import (
    ReplayMarketDataArtifact,
    ReplayRestResponse,
    build_replay_market_data_artifact,
    load_replay_market_data_artifact,
    write_replay_market_data_artifact,
)
from test_replay_inputs import _context


def _request() -> DevReplayDataRequest:
    return build_dev_replay_data_request(**_context())


def _klines(request: DevReplayDataRequest) -> list[list[Any]]:
    start = int(request.start.timestamp() * 1000)
    return [[
        start + index * 60000, "100", "101", "99", "100", "2",
        start + (index + 1) * 60000 - 1, "200", 2, "1", "100", "0",
    ] for index in range(request.expected_candle_count)]


def _funding(request: DevReplayDataRequest) -> list[dict[str, Any]]:
    return [{
        "symbol": request.request.armed.symbol,
        "fundingTime": int((request.start + timedelta(hours=2)).timestamp() * 1000),
        "fundingRate": "-0.0001", "markPrice": "100", "rateType": "Regular",
    }]


def _response(
    root: Path, request: DevReplayDataRequest, body: Any,
    *, endpoint: Literal["/fapi/v1/klines", "/fapi/v1/fundingRate"] = "/fapi/v1/klines",
    name: str = "response.json", start_ms: int | None = None, end_ms: int | None = None,
    limit: int = 1000,
) -> ReplayRestResponse:
    content = json.dumps(body).encode()
    (root / name).write_bytes(content)
    return ReplayRestResponse(
        endpoint=endpoint, symbol=request.request.armed.symbol,
        interval="1m" if endpoint == "/fapi/v1/klines" else None,
        start_time_ms=int(request.start.timestamp() * 1000) if start_ms is None else start_ms,
        end_time_ms=int(request.end_exclusive.timestamp() * 1000) - 1 if end_ms is None else end_ms,
        limit=limit, observed_at=request.end_exclusive + timedelta(days=1),
        relative_path=name, body_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_complete_one_minute_responses_round_trip_with_decimal_prices(tmp_path: Path) -> None:
    request = _request()
    response = _response(tmp_path, request, _klines(request))
    artifact, candles = build_replay_market_data_artifact(
        project_dir=tmp_path, request=request, role="CANDLE_ONE_MINUTE", responses=(response,),
    )
    assert isinstance(candles, pd.DataFrame)
    assert artifact.record_count == len(candles) == 526
    assert candles["source"].unique().tolist() == ["REST"]
    assert candles["symbol"].unique().tolist() == ["AAAUSDT"]
    assert candles["source_file_hash"].unique().tolist() == [response.body_sha256]
    path = write_replay_market_data_artifact(artifact, tmp_path)
    assert write_replay_market_data_artifact(artifact, tmp_path) == path
    assert ReplayMarketDataArtifact.model_validate_json(path.read_bytes()) == artifact
    pd.testing.assert_frame_equal(candles, load_replay_market_data_artifact(
        project_dir=tmp_path, request=request, artifact=artifact,
    ))
    (tmp_path / response.relative_path).write_bytes(b"[]")
    with pytest.raises(ReplayDataInputError, match="file hash mismatch"):
        load_replay_market_data_artifact(project_dir=tmp_path, request=request, artifact=artifact)


def test_multiple_pages_require_contiguous_queries_and_data(tmp_path: Path) -> None:
    request = _request()
    rows = _klines(request)
    cut = rows[200][0]
    first = _response(tmp_path, request, rows[:200], name="first.json", end_ms=cut - 1)
    second = _response(tmp_path, request, rows[200:], name="second.json", start_ms=cut)
    artifact, _ = build_replay_market_data_artifact(
        project_dir=tmp_path, request=request, role="CANDLE_ONE_MINUTE", responses=(first, second),
    )
    assert artifact.record_count == 526
    for pages in ((second, first), (first,), (first, first)):
        with pytest.raises(ReplayDataInputError):
            build_replay_market_data_artifact(
                project_dir=tmp_path, request=request, role="CANDLE_ONE_MINUTE", responses=pages,
            )


@pytest.mark.parametrize("kind", ["missing", "duplicate", "reversed", "empty", "api_error"])
def test_incomplete_or_invalid_kline_response_is_not_repaired(tmp_path: Path, kind: str) -> None:
    request = _request()
    rows: Any = _klines(request)
    if kind == "missing":
        rows.pop()
    elif kind == "duplicate":
        rows[10] = rows[9]
    elif kind == "reversed":
        rows.reverse()
    elif kind == "empty":
        rows = []
    else:
        rows = {"code": -1003, "msg": "synthetic rate limit"}
    response = _response(tmp_path, request, rows)
    with pytest.raises(ReplayDataInputError):
        build_replay_market_data_artifact(
            project_dir=tmp_path, request=request, role="CANDLE_ONE_MINUTE", responses=(response,),
        )


@pytest.mark.parametrize(("position", "value"), [
    (0, True), (6, 1), (8, -1), (1, 1.1), (1, "0"), (2, "98"),
    (3, "102"), (4, "NaN"), (5, "-1"), (7, None),
])
def test_invalid_financial_or_timestamp_fields_fail_closed(
    tmp_path: Path, position: int, value: Any,
) -> None:
    request = _request()
    rows = _klines(request)
    rows[0][position] = value
    response = _response(tmp_path, request, rows)
    with pytest.raises((ReplayDataInputError, CandleValidationError)):
        build_replay_market_data_artifact(
            project_dir=tmp_path, request=request, role="CANDLE_ONE_MINUTE", responses=(response,),
        )


def test_funding_schedule_comes_from_rows_not_an_eight_hour_constant(tmp_path: Path) -> None:
    request = _request()
    rows = _funding(request)
    rows.append({**rows[0], "fundingTime": rows[0]["fundingTime"] + 3600000})
    response = _response(tmp_path, request, rows, endpoint="/fapi/v1/fundingRate")
    artifact, data = build_replay_market_data_artifact(
        project_dir=tmp_path, request=request, role="FUNDING", responses=(response,),
    )
    assert isinstance(data, FundingDataset)
    assert len(data.expected_settlement_times) == 2
    assert data.observations[0].rate is not None and data.observations[0].rate < 0
    assert artifact.research_authorized is False
    assert load_replay_market_data_artifact(
        project_dir=tmp_path, request=request, artifact=artifact,
    ) == data


@pytest.mark.parametrize(("key", "value"), [
    ("symbol", "OTHERUSDT"), ("fundingTime", True), ("fundingTime", 1),
    ("fundingRate", None), ("fundingRate", "NaN"), ("markPrice", None),
    ("markPrice", "0"), ("rateType", "Special"), ("rateType", "Unknown"),
])
def test_missing_or_unsupported_funding_is_never_zero_filled(
    tmp_path: Path, key: str, value: Any,
) -> None:
    request = _request()
    rows = _funding(request)
    rows[0][key] = value
    response = _response(tmp_path, request, rows, endpoint="/fapi/v1/fundingRate")
    with pytest.raises(ValueError):
        build_replay_market_data_artifact(
            project_dir=tmp_path, request=request, role="FUNDING", responses=(response,),
        )


@pytest.mark.parametrize("kind", ["empty", "truncated", "duplicate"])
def test_funding_needs_nonempty_untruncated_unique_evidence(tmp_path: Path, kind: str) -> None:
    request = _request()
    rows = [] if kind == "empty" else _funding(request)
    if kind == "duplicate":
        rows *= 2
    response = _response(
        tmp_path, request, rows, endpoint="/fapi/v1/fundingRate",
        limit=1 if kind == "truncated" else 1000,
    )
    with pytest.raises(ReplayDataInputError):
        build_replay_market_data_artifact(
            project_dir=tmp_path, request=request, role="FUNDING", responses=(response,),
        )


def test_duplicate_json_keys_and_unclosed_history_are_rejected(tmp_path: Path) -> None:
    request = _request()
    response = _response(tmp_path, request, _funding(request), endpoint="/fapi/v1/fundingRate")
    content = b'[{"symbol":"AAAUSDT","symbol":"OTHERUSDT"}]'
    (tmp_path / response.relative_path).write_bytes(content)
    response = response.model_copy(update={"body_sha256": hashlib.sha256(content).hexdigest()})
    with pytest.raises(ReplayDataInputError, match="duplicate JSON"):
        build_replay_market_data_artifact(
            project_dir=tmp_path, request=request, role="FUNDING", responses=(response,),
        )
    response = response.model_copy(update={"observed_at": request.start})
    with pytest.raises(ReplayDataInputError, match="closed requested history"):
        build_replay_market_data_artifact(
            project_dir=tmp_path, request=request, role="FUNDING", responses=(response,),
        )


@pytest.mark.parametrize("relative", ["../body", "/body", "C:/body", "body:ads", "a\\b", "."])
def test_unsafe_response_paths_are_rejected(tmp_path: Path, relative: str) -> None:
    response = _response(tmp_path, _request(), [])
    payload = response.model_dump(mode="json")
    payload["relative_path"] = relative
    with pytest.raises(ValidationError):
        ReplayRestResponse.model_validate(payload)
