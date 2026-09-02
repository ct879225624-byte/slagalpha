"""Synthetic full-set byte checks; no real scanning, exchange calls or P7 execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.replay_inputs import ReplayDataInputError
from slagalpha.research.replay_market_data import build_replay_market_data_artifact
from slagalpha.research.request_set import DevReplayRequestSet, build_dev_replay_request_set
from slagalpha.research.request_set_market_data import (
    DevRequestMarketDataPair,
    DevRequestSetMarketDataReport,
    require_request_set_market_data_report_binding,
    verify_dev_request_set_market_data,
    write_request_set_market_data_report,
)
from test_replay_market_data import _funding, _klines, _response
from test_request_set import _rehash, _replace_record, _request_set_context
from test_research_splits import _hash


def _market_set_context(root: Path) -> dict[str, Any]:
    context = _request_set_context()
    request_set = build_dev_replay_request_set(**context)
    pairs = []
    for index, request in enumerate(request_set.requests):
        minute = _response(root, request, _klines(request), name=f"minutes-{index}.json")
        funding = _response(
            root, request, _funding(request), name=f"funding-{index}.json",
            endpoint="/fapi/v1/fundingRate",
        )
        minute_artifact, _ = build_replay_market_data_artifact(
            project_dir=root, request=request, role="CANDLE_ONE_MINUTE", responses=(minute,),
        )
        funding_artifact, _ = build_replay_market_data_artifact(
            project_dir=root, request=request, role="FUNDING", responses=(funding,),
        )
        pairs.append(DevRequestMarketDataPair(
            request_hash=request.request_hash, one_minute=minute_artifact, funding=funding_artifact,
        ))
    return {**context, "project_dir": root, "request_set": request_set, "pairs": tuple(pairs)}


def test_complete_set_revalidates_every_pair_without_authorizing_research(tmp_path: Path) -> None:
    context = _market_set_context(tmp_path)
    report = verify_dev_request_set_market_data(**context)
    assert report.verified_request_count == 2
    assert report.one_minute_record_count == 1052
    assert report.funding_record_count == 2
    assert report.strategy_evidence_verified is report.replay_executed is False
    assert report.research_authorized is False
    assert verify_dev_request_set_market_data(**context) == report
    path = write_request_set_market_data_report(report, tmp_path)
    assert write_request_set_market_data_report(report, tmp_path) == path
    assert DevRequestSetMarketDataReport.model_validate_json(path.read_bytes()) == report
    context.pop("pairs")
    require_request_set_market_data_report_binding(report, **context)
    path.write_bytes(b"changed")
    with pytest.raises(ReplayDataInputError, match="changed"):
        write_request_set_market_data_report(report, tmp_path)


@pytest.mark.parametrize("mode", ["missing", "extra", "duplicate", "reversed", "empty"])
def test_full_pair_coverage_rejected_before_opening_any_file(tmp_path: Path, mode: str) -> None:
    context = _market_set_context(tmp_path)
    first, second = context["pairs"]
    context["pairs"] = {
        "missing": (first,), "extra": (first, second, first), "duplicate": (first, first),
        "reversed": (second, first), "empty": (),
    }[mode]
    context["project_dir"] = tmp_path / "nonexistent"
    with pytest.raises(ReplayDataInputError, match="exactly cover"):
        verify_dev_request_set_market_data(**context)


def test_cross_request_or_role_mixing_rejected_before_file_access(tmp_path: Path) -> None:
    context = _market_set_context(tmp_path)
    first, second = context["pairs"]
    context["project_dir"] = tmp_path / "nonexistent"
    for updates, message in (({"funding": second.funding}, "same request"),
                             ({"funding": first.one_minute}, "one Funding")):
        context["pairs"] = (first.model_copy(update=updates), second)
        with pytest.raises(ValidationError, match=message):
            verify_dev_request_set_market_data(**context)


@pytest.mark.parametrize("role", ["minutes", "funding"])
def test_last_request_tampering_invalidates_saved_report(tmp_path: Path, role: str) -> None:
    context = _market_set_context(tmp_path)
    report = verify_dev_request_set_market_data(**context)
    context.pop("pairs")
    (tmp_path / f"{role}-1.json").write_bytes(b"[]")
    with pytest.raises(ReplayDataInputError, match="file hash mismatch"):
        require_request_set_market_data_report_binding(report, **context)
    assert not (tmp_path / "manifests").exists()  # Failed validation publishes no partial receipt.


def test_valid_rehashed_subset_of_requests_and_pairs_still_fails_before_io(tmp_path: Path) -> None:
    context = _market_set_context(tmp_path)
    payload = context["request_set"].model_dump(mode="json")
    payload["requests"] = payload["requests"][:1]
    context["request_set"] = DevReplayRequestSet.model_validate(_rehash(payload))
    context["pairs"] = context["pairs"][:1]
    context["project_dir"] = tmp_path / "nonexistent"
    with pytest.raises(ReplayDataInputError, match="complete scan evidence"):
        verify_dev_request_set_market_data(**context)


def test_unverified_rules_and_changed_scan_sources_block_before_io(tmp_path: Path) -> None:
    context = _market_set_context(tmp_path)
    context["project_dir"] = tmp_path / "nonexistent"
    registry = context["registry"]
    context["registry"] = ContractRegistry(
        registry_version=registry.registry_version,
        entries=(registry.entries[0].model_copy(update={
            "verification_status": RegistryVerification.UNVERIFIED,
        }),),
    )
    with pytest.raises(ValueError, match="audit|blocked"):
        verify_dev_request_set_market_data(**context)
    context["registry"] = registry
    _replace_record(context, 0, 1, upstream_evidence_hash=_hash("changed"))
    with pytest.raises(ReplayDataInputError, match="complete scan evidence"):
        verify_dev_request_set_market_data(**context)


def test_normalized_hash_is_recomputed_not_trusted_from_artifact(tmp_path: Path) -> None:
    context = _market_set_context(tmp_path)
    first, second = context["pairs"]
    payload = second.one_minute.model_dump(mode="json")
    payload["normalized_content_hash"] = "0" * 64
    payload.pop("artifact_hash")
    payload["artifact_hash"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    forged = type(second.one_minute).model_validate(payload)
    context["pairs"] = (first, second.model_copy(update={"one_minute": forged}))
    with pytest.raises(ReplayDataInputError, match="no longer matches"):
        verify_dev_request_set_market_data(**context)


def test_report_request_set_hash_mismatch_rejected_without_reading_files(tmp_path: Path) -> None:
    context = _market_set_context(tmp_path)
    payload = verify_dev_request_set_market_data(**context).model_dump(mode="json")
    payload["request_set_hash"] = "0" * 64
    payload.pop("report_hash")
    payload["report_hash"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    report = DevRequestSetMarketDataReport.model_validate(payload)
    context.pop("pairs")
    context["project_dir"] = tmp_path / "nonexistent"
    with pytest.raises(ReplayDataInputError, match="different request set"):
        require_request_set_market_data_report_binding(report, **context)


@pytest.mark.parametrize("updates", [
    {"verified_request_count": 1}, {"one_minute_record_count": 1}, {"funding_record_count": 1},
    {"strategy_evidence_verified": True}, {"replay_executed": True},
    {"research_authorized": True}, {"report_hash": "0" * 64},
])
def test_invalid_report_counts_and_authority_rejected(
    tmp_path: Path, updates: dict[str, Any],
) -> None:
    report = verify_dev_request_set_market_data(**_market_set_context(tmp_path))
    payload = report.model_dump(mode="json")
    with pytest.raises(ValidationError):
        DevRequestSetMarketDataReport.model_validate({**payload, **updates})
