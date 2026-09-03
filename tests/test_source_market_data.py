"""Day recovery is mocked explicitly; 1m/Funding validation reads actual synthetic JSON files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.replay_inputs import ReplayDataInputError
from slagalpha.research.request_set import DevReplayRequestSet, DevScanDayEvidence
from slagalpha.research.request_set_market_data import verify_dev_request_set_market_data
from slagalpha.research.source_request_set import (
    require_source_bound_market_data_report,
    verify_source_bound_request_set_market_data,
)
from test_request_set import _rehash
from test_request_set_market_data import _market_set_context
from test_source_request_set import _lineage_histories


@pytest.fixture
def market_context(tmp_path: Path) -> dict[str, Any]:
    return _market_set_context(tmp_path)


@pytest.fixture
def restored_days(
    market_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def mock_restore(**kwargs: Any) -> DevScanDayEvidence:
        calls.append(kwargs)
        day: DevScanDayEvidence = next(item for item in market_context["evidence"]
                                      if item.content_hash == kwargs["content_hash"])
        return day

    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        mock_restore)
    return calls


def _arguments(context: dict[str, Any], *, report: bool = False) -> dict[str, Any]:
    excluded = ("evidence", "pairs") if report else ("evidence",)
    return {key: value for key, value in context.items() if key not in excluded}


def test_source_recovery_then_all_synthetic_market_bytes_are_revalidated(
    market_context: dict[str, Any], restored_days: list[dict[str, Any]],
) -> None:
    report = verify_source_bound_request_set_market_data(**_arguments(market_context))
    assert len(restored_days) == 2
    assert report.verified_request_count == 2
    assert report.one_minute_record_count == 1052
    assert report.funding_record_count == 2
    # Conservative existing report schema stays unchanged; it is not a cached source proof.
    assert report == verify_dev_request_set_market_data(**market_context)
    assert report.strategy_evidence_verified is report.research_authorized is False
    assert report.replay_executed is False
    require_source_bound_market_data_report(report, **_arguments(market_context, report=True))
    assert len(restored_days) == 4
    assert restored_days[0]["history_lineage"] is restored_days[1]["history_lineage"]
    assert restored_days[2]["history_lineage"] is restored_days[3]["history_lineage"]
    assert restored_days[0]["history_lineage"] is not restored_days[2]["history_lineage"]


@pytest.mark.parametrize("failed_day", [0, 1])
def test_source_failure_blocks_before_market_response_loader(
    market_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failed_day: int,
) -> None:
    calls: list[dict[str, Any]] = []

    def fail_source(**kwargs: Any) -> DevScanDayEvidence:
        index = len(calls)
        calls.append(kwargs)
        if index == failed_day:
            raise CandleInputError("simulated changed original NO_SIGNAL source")
        day: DevScanDayEvidence = market_context["evidence"][index]
        return day

    def forbidden_market_loader(**kwargs: Any) -> None:
        raise AssertionError("market response opened before all scan source validation")

    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        fail_source)
    monkeypatch.setattr("slagalpha.research.request_set_market_data.load_replay_market_data_artifact",
                        forbidden_market_loader)
    with pytest.raises(CandleInputError, match="NO_SIGNAL source"):
        verify_source_bound_request_set_market_data(**_arguments(market_context))
    assert len(calls) == failed_day + 1


@pytest.mark.parametrize("role", ["minutes", "funding"])
def test_saved_report_does_not_hide_later_market_byte_changes(
    market_context: dict[str, Any], restored_days: list[dict[str, Any]], role: str,
) -> None:
    report = verify_source_bound_request_set_market_data(**_arguments(market_context))
    (market_context["project_dir"] / f"{role}-1.json").write_bytes(b"[]")
    with pytest.raises(ReplayDataInputError, match="file hash mismatch"):
        require_source_bound_market_data_report(report, **_arguments(market_context, report=True))
    assert len(restored_days) == 4


@pytest.mark.parametrize("change", ["missing", "duplicate", "reversed", "empty"])
def test_market_pair_coverage_is_still_exact(
    market_context: dict[str, Any], restored_days: list[dict[str, Any]], change: str,
) -> None:
    first, second = market_context["pairs"]
    market_context["pairs"] = {"missing": (first,), "duplicate": (first, first),
                               "reversed": (second, first), "empty": ()}[change]
    with pytest.raises(ReplayDataInputError, match="exactly cover"):
        verify_source_bound_request_set_market_data(**_arguments(market_context))
    assert len(restored_days) == 2


def test_old_declared_report_cannot_replace_missing_source_checkpoints(
    market_context: dict[str, Any],
) -> None:
    report = verify_dev_request_set_market_data(**market_context)
    with pytest.raises(FileNotFoundError):
        require_source_bound_market_data_report(report, **_arguments(market_context, report=True))


def test_current_unverified_rules_stop_before_daily_recovery(
    market_context: dict[str, Any], restored_days: list[dict[str, Any]],
) -> None:
    registry = market_context["registry"]
    market_context["registry"] = ContractRegistry(
        registry_version=registry.registry_version, entries=(
            registry.entries[0].model_copy(update={
                "verification_status": RegistryVerification.UNVERIFIED,
            }),
        ),
    )
    with pytest.raises(ValueError, match="audit|blocked"):
        verify_source_bound_request_set_market_data(**_arguments(market_context))
    assert restored_days == []


def test_rehashed_request_and_pair_subset_is_not_a_source_validated_success(
    market_context: dict[str, Any], restored_days: list[dict[str, Any]],
) -> None:
    payload = market_context["request_set"].model_dump(mode="json")
    payload["requests"] = payload["requests"][:1]
    market_context["request_set"] = DevReplayRequestSet.model_validate(_rehash(payload))
    market_context["pairs"] = market_context["pairs"][:1]
    with pytest.raises(ReplayDataInputError, match="complete scan evidence"):
        verify_source_bound_request_set_market_data(**_arguments(market_context))
    assert len(restored_days) == 2


def test_matching_saved_report_still_rechecks_changed_scan_sources(
    market_context: dict[str, Any], restored_days: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = verify_source_bound_request_set_market_data(**_arguments(market_context))

    def changed_source(**kwargs: Any) -> DevScanDayEvidence:
        raise CandleInputError("simulated source changed after report was saved")

    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        changed_source)
    with pytest.raises(CandleInputError, match="after report"):
        require_source_bound_market_data_report(report, **_arguments(market_context, report=True))


def test_report_for_another_request_set_fails_before_source_recovery(
    market_context: dict[str, Any], restored_days: list[dict[str, Any]],
) -> None:
    import hashlib

    from slagalpha.reporting.run_manifest import canonical_json_bytes
    from slagalpha.research.request_set_market_data import DevRequestSetMarketDataReport

    payload = verify_dev_request_set_market_data(**market_context).model_dump(mode="json")
    payload["request_set_hash"] = "0" * 64
    payload.pop("report_hash")
    report = DevRequestSetMarketDataReport.model_validate({
        **payload, "report_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    with pytest.raises(ReplayDataInputError, match="different request set"):
        require_source_bound_market_data_report(report, **_arguments(market_context, report=True))
    assert restored_days == []


@pytest.mark.parametrize("change", ["origin", "partition"])
@pytest.mark.parametrize("reuse", [False, True])
def test_cross_day_lineage_drift_fails_before_any_market_response_read(
    market_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str, reuse: bool,
) -> None:
    # A matching declaration-based report cannot substitute for cross-day source validation.
    report = verify_dev_request_set_market_data(**market_context)
    histories = _lineage_histories(market_context, change)
    calls: list[dict[str, Any]] = []

    def mock_restore(**kwargs: Any) -> DevScanDayEvidence:
        index = len(calls)
        calls.append(kwargs)
        kwargs["history_lineage"].require(histories[index])
        day: DevScanDayEvidence = market_context["evidence"][index]
        return day

    def forbidden_market_loader(**kwargs: Any) -> None:
        raise AssertionError("cross-day source lineage must pass before any market input is read")

    monkeypatch.setattr("slagalpha.research.source_request_set.restore_source_bound_scan_day",
                        mock_restore)
    monkeypatch.setattr("slagalpha.research.request_set_market_data.load_replay_market_data_artifact",
                        forbidden_market_loader)
    with pytest.raises(CandleInputError, match="origin changed|partition receipt changed"):
        if reuse:
            require_source_bound_market_data_report(
                report, **_arguments(market_context, report=True),
            )
        else:
            verify_source_bound_request_set_market_data(**_arguments(market_context))
    assert len(calls) == 2 and calls[0]["history_lineage"] is calls[1]["history_lineage"]
