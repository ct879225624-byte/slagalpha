from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from slagalpha.backtest.analytics import FundingDataset
from slagalpha.research.batch_replay import _load_ready_data
from slagalpha.research.funding_mark_supplement import (
    FundingMarkSupplementError,
    fetch_funding_mark_event,
    verify_funding_mark_supplement,
)
from slagalpha.research.replay_market_data import build_replay_market_data_artifact
from slagalpha.research.replay_prep import prepare_dev_replay
from test_replay_market_data import _funding
from test_replay_prep import _first, _fixture, _replace


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_native_funding_mark_stops_without_mark_kline(tmp_path: Path) -> None:
    funding_time = datetime(2024, 1, 1, 2, 15, tzinfo=UTC)
    funding_ms = int(funding_time.timestamp() * 1000)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "ETHUSDT",
                    "fundingTime": funding_ms,
                    "fundingRate": "-0.0001",
                    "markPrice": "100.2",
                }
            ],
            request=request,
        )

    with _client(handler) as client:
        artifact = fetch_funding_mark_event(
            client=client,
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            request_hash="a" * 64,
            funding_artifact_hash="b" * 64,
            symbol="ETHUSDT",
            funding_time=funding_time,
            funding_rate=Decimal("-0.0001"),
            sleep=lambda _: None,
        )
    assert artifact.verification_status == "NATIVE"
    assert artifact.native_mark_price == Decimal("100.2")
    assert calls == ["/fapi/v1/fundingRate"]
    assert verify_funding_mark_supplement(project_dir=tmp_path, artifact=artifact) == Decimal(
        "100.2"
    )


def test_exact_mark_kline_resolves_replayprep_without_mutating_original(tmp_path: Path) -> None:
    report, artifacts, schedules = _fixture(tmp_path)
    funding = _first(artifacts, "FUNDING")
    request = report.accepted_requests[0].request
    rows = _funding(request)
    rows[0]["markPrice"] = ""
    response = funding.responses[0]
    content = json.dumps(rows, separators=(",", ":")).encode()
    (tmp_path / response.relative_path).write_bytes(content)
    changed_response = response.model_copy(
        update={"body_sha256": hashlib.sha256(content).hexdigest()}
    )
    changed, original_data = build_replay_market_data_artifact(
        project_dir=tmp_path,
        request=request,
        role="FUNDING",
        responses=(changed_response,),
    )
    assert isinstance(original_data, FundingDataset)
    assert original_data.observations[0].mark_price is None
    assert original_data.observations[0].rate is not None
    funding_rate = original_data.observations[0].rate
    funding_time = original_data.observations[0].settlement_time
    funding_ms = int(funding_time.timestamp() * 1000)

    def handler(request_: httpx.Request) -> httpx.Response:
        if request_.url.path.endswith("fundingRate"):
            body: list[Any] = [
                {
                    "symbol": request.request.armed.symbol,
                    "fundingTime": funding_ms,
                    "fundingRate": str(funding_rate),
                    "markPrice": "",
                }
            ]
        else:
            body = [
                [
                    funding_ms,
                    "100",
                    "101",
                    "99",
                    "100.5",
                    "0",
                    funding_ms + 59999,
                    "0",
                    1,
                    "0",
                    "0",
                    "0",
                ]
            ]
        return httpx.Response(200, json=body, request=request_)

    with _client(handler) as client:
        supplement = fetch_funding_mark_event(
            client=client,
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            request_hash=request.request_hash,
            funding_artifact_hash=changed.artifact_hash,
            symbol=request.request.armed.symbol,
            funding_time=funding_time,
            funding_rate=funding_rate,
            sleep=lambda _: None,
        )
    result = prepare_dev_replay(
        project_dir=tmp_path,
        report=report,
        market_data=_replace(artifacts, changed),
        funding_schedules=schedules,
        funding_mark_supplements=(supplement,),
    )
    assert result.summary.can_start_dev_replay is True
    ready = result.manifest.inputs[0]
    assert ready.funding_mark_supplement_hashes == (supplement.artifact_hash,)
    assert ready.funding_mark_warnings == ("DEV_APPROXIMATE_FUNDING_MARK",)
    loaded_candles, loaded_funding = _load_ready_data(
        project_dir=tmp_path,
        ready=ready,
        artifacts={item.artifact_hash: item for item in _replace(artifacts, changed)},
        supplements={supplement.artifact_hash: supplement},
    )
    assert len(loaded_candles) == request.expected_candle_count
    assert loaded_funding.observations[0].mark_price == Decimal("100")


def test_mark_kline_time_mismatch_fails_closed(tmp_path: Path) -> None:
    funding_time = datetime(2024, 1, 1, 2, 15, tzinfo=UTC)
    funding_ms = int(funding_time.timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("fundingRate"):
            body: list[Any] = [
                {
                    "symbol": "ETHUSDT",
                    "fundingTime": funding_ms,
                    "fundingRate": "-0.0001",
                    "markPrice": "",
                }
            ]
        else:
            body = [
                [
                    funding_ms + 60000,
                    "100",
                    "101",
                    "99",
                    "100.5",
                    "0",
                    funding_ms + 119999,
                    "0",
                    1,
                    "0",
                    "0",
                    "0",
                ]
            ]
        return httpx.Response(200, json=body, request=request)

    with _client(handler) as client, pytest.raises(FundingMarkSupplementError):
        fetch_funding_mark_event(
            client=client,
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            request_hash="a" * 64,
            funding_artifact_hash="b" * 64,
            symbol="ETHUSDT",
            funding_time=funding_time,
            funding_rate=Decimal("-0.0001"),
            sleep=lambda _: None,
        )
