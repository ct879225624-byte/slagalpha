"""Explicitly synthetic evidence: no fixture demonstrates historical rule authenticity."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.data.exchange_info import parse_exchange_info
from slagalpha.data.rule_evidence import (
    RuleEvidenceError,
    RuleEvidenceIntakeReport,
    RuleEvidenceSubmission,
    audit_rule_evidence,
    load_rule_evidence_submission,
    write_rule_evidence_report,
)
from test_exchange_info import exchange_payload
from test_research_splits import _unverified_registry


def _submission(tmp_path: Path) -> RuleEvidenceSubmission:
    raw = exchange_payload()
    observed = datetime(2024, 2, 1, tzinfo=UTC)
    parsed = parse_exchange_info(raw, fetched_at=observed).contracts[0]
    original = _unverified_registry().entries[0]
    candidate = original.model_copy(update={
        **parsed.model_dump(exclude={"pair", "delivery_date"}),
        "derived_first_candle_at": parsed.onboard_date,
        "effective_from": observed,
        "effective_to": observed + timedelta(days=2),
        "source_snapshot_hash": hashlib.sha256(raw).hexdigest(),
        "source_ref": "fixture:synthetic-snapshot-not-market-evidence",
    })
    continuity = b"MOCK continuity proof for code tests only; not authentic market evidence."
    (tmp_path / "snapshot.json").write_bytes(raw)
    (tmp_path / "continuity.txt").write_bytes(continuity)
    return RuleEvidenceSubmission(
        candidate=candidate,
        snapshot_file="snapshot.json",
        snapshot_observed_at=observed,
        collected_at=observed + timedelta(days=3),
        continuity_file="continuity.txt",
        continuity_sha256=hashlib.sha256(continuity).hexdigest(),
        continuity_note="MOCK interval proof; a real intake still needs independent review.",
    )


def _change_candidate(submission: RuleEvidenceSubmission, **updates: Any) -> RuleEvidenceSubmission:
    return RuleEvidenceSubmission.model_validate({
        **submission.model_dump(),
        "candidate": {**submission.candidate.model_dump(), **updates},
    })


def test_complete_documents_are_only_ready_for_review_and_do_not_modify_registry(
    tmp_path: Path,
) -> None:
    submission = _submission(tmp_path)
    original = submission.model_dump_json()
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.status == "READY_FOR_REVIEW"
    assert report.blockers == ()
    assert report.verification_status == "UNVERIFIED"
    assert report.registry_modified is report.research_authorized is False
    assert "INTERVAL_CONTINUITY" in report.manual_review_checks
    assert "HISTORICAL_CAPTURE_TIMESTAMP" in report.manual_review_checks
    assert submission.model_dump_json() == original
    assert submission.candidate.eligible_for_locked_research is False
    assert audit_rule_evidence(submission, evidence_root=tmp_path) == report
    output = write_rule_evidence_report(report, tmp_path / "data")
    assert RuleEvidenceIntakeReport.model_validate_json(output.read_bytes()) == report
    assert write_rule_evidence_report(report, tmp_path / "data") == output
    assert not (tmp_path / "data" / "manifests" / "contract_registry").exists()
    output.write_text("{}", encoding="utf-8")
    with pytest.raises(RuleEvidenceError, match="changed"):
        write_rule_evidence_report(report, tmp_path / "data")


def test_missing_continuity_and_open_ended_current_rules_fail_closed(tmp_path: Path) -> None:
    submission = _change_candidate(_submission(tmp_path), effective_to=None)
    submission = submission.model_copy(update={"continuity_file": None})
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.status == "BLOCKED"
    assert report.blockers == ("CLOSED_INTERVAL_REQUIRED", "CONTINUITY_EVIDENCE_REQUIRED")


@pytest.mark.parametrize("observed", [
    datetime(2024, 1, 31, tzinfo=UTC),
    datetime(2024, 2, 3, tzinfo=UTC),
    datetime(2026, 1, 1, tzinfo=UTC),
])
def test_snapshot_before_interval_at_end_or_current_cannot_anchor_history(
    tmp_path: Path, observed: datetime,
) -> None:
    submission = _submission(tmp_path).model_copy(update={
        "snapshot_observed_at": observed, "collected_at": datetime(2026, 2, 1, tzinfo=UTC),
    })
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert "SNAPSHOT_OUTSIDE_CLAIMED_INTERVAL" in report.blockers


@pytest.mark.parametrize(("field", "value"), [
    ("tick_size", "0.02"), ("step_size", "0.01"), ("base_asset", "OTHER"),
    ("status", "SETTLING"), ("min_qty", None), ("max_qty", "2000"),
    ("min_notional", "10"), ("onboard_date", None),
])
def test_identity_and_all_filter_values_must_match_raw_snapshot(
    tmp_path: Path, field: str, value: Any,
) -> None:
    submission = _change_candidate(_submission(tmp_path), **{field: value})
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.blockers == ("SNAPSHOT_METADATA_MISMATCH",)
    assert report.mismatched_fields == (field,)


def test_unknown_symbol_does_not_fall_back_to_another_contract(tmp_path: Path) -> None:
    submission = _change_candidate(_submission(tmp_path), symbol="UNKNOWNUSDT")
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.blockers == ("SNAPSHOT_SYMBOL_MISSING",)


@pytest.mark.parametrize("raw", [
    b"not json", b"[]", b'{"symbols":[],"symbols":[],"serverTime":1706745600000}',
    exchange_payload().replace(b'"LOT_SIZE"', b'"MARKET_LOT_SIZE"'),
    exchange_payload().replace(b'"tickSize":"0.10"', b'"tickSize":"NaN"'),
])
def test_invalid_ambiguous_or_incomplete_raw_snapshot_is_not_accepted(
    tmp_path: Path, raw: bytes,
) -> None:
    submission = _change_candidate(
        _submission(tmp_path), source_snapshot_hash=hashlib.sha256(raw).hexdigest()
    )
    (tmp_path / "snapshot.json").write_bytes(raw)
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.blockers == ("SNAPSHOT_INVALID",)


@pytest.mark.parametrize("name", [
    "../outside.json", "a/../../outside.json", "/outside.json", "C:/outside.json",
    "sub\\snapshot.json", "snapshot.json:stream", "./snapshot.json", "",
])
def test_paths_must_stay_inside_declared_evidence_directory(tmp_path: Path, name: str) -> None:
    submission = _submission(tmp_path).model_copy(update={"snapshot_file": name})
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.blockers == ("SNAPSHOT_PATH_UNSAFE",)
    assert report.snapshot_actual_sha256 is None


def test_resolved_symlink_escape_is_rejected_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    submission = _submission(tmp_path).model_copy(update={"snapshot_file": "link.json"})
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path.name == "link.json":
            return tmp_path.parent / "outside.json"
        return original_resolve(path, strict=strict)

    # Mock resolution only: does not require Windows symlink privileges or read outside tmp_path.
    monkeypatch.setattr(Path, "resolve", resolve)
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.blockers == ("SNAPSHOT_PATH_UNSAFE",)
    assert report.snapshot_actual_sha256 is None


def test_missing_and_changed_files_are_reported_without_parsing_them(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    missing = submission.model_copy(update={"snapshot_file": "missing.json"})
    assert audit_rule_evidence(missing, evidence_root=tmp_path).blockers == (
        "SNAPSHOT_FILE_MISSING",
    )
    (tmp_path / "snapshot.json").write_bytes(b"changed invalid content")
    (tmp_path / "continuity.txt").write_bytes(b"changed proof")
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    assert report.blockers == ("CONTINUITY_HASH_MISMATCH", "SNAPSHOT_HASH_MISMATCH")


@pytest.mark.parametrize("content", [b"", b"   ", exchange_payload()])
def test_empty_or_snapshot_only_continuity_evidence_is_blocked(
    tmp_path: Path, content: bytes,
) -> None:
    submission = _submission(tmp_path)
    (tmp_path / "continuity.txt").write_bytes(content)
    submission = submission.model_copy(update={
        "continuity_sha256": hashlib.sha256(content).hexdigest(),
    })
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    expected = "CONTINUITY_EVIDENCE_EMPTY" if not content.strip() else (
        "SNAPSHOT_ALONE_IS_NOT_CONTINUITY_EVIDENCE"
    )
    assert report.blockers == (expected,)


def test_submission_and_report_cannot_skip_review_or_reuse_tampered_hashes(tmp_path: Path) -> None:
    submission = _submission(tmp_path)
    with pytest.raises(ValueError, match="UNVERIFIED candidates only"):
        _change_candidate(submission, verification_status="VERIFIED")
    for updates in (
        {"snapshot_observed_at": datetime(2024, 2, 1)},
        {"collected_at": datetime(2024, 1, 31, tzinfo=UTC)},
        {"continuity_note": " "},
    ):
        with pytest.raises(ValueError):
            RuleEvidenceSubmission.model_validate({**submission.model_dump(), **updates})
    report = audit_rule_evidence(submission, evidence_root=tmp_path)
    report_changes: tuple[dict[str, Any], ...] = (
        {"manual_review_checks": []}, {"research_authorized": True},
        {"submission_hash": "f" * 64}, {"verification_status": "VERIFIED"},
    )
    for report_updates in report_changes:
        with pytest.raises(ValueError):
            RuleEvidenceIntakeReport.model_validate({**report.model_dump(), **report_updates})
    with pytest.raises(RuleEvidenceError, match="duplicate JSON key"):
        load_rule_evidence_submission(b'{"candidate":{},"candidate":{}}')


def test_cli_exit_codes_schema_and_no_sensitive_invalid_input_echo(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "p9_rule_evidence_audit.py"

    def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *arguments], capture_output=True, text=True,
            check=False, timeout=30,
        )

    submission = _submission(tmp_path)
    path = tmp_path / "submission.json"
    path.write_text(submission.model_dump_json(), encoding="utf-8")
    arguments = [str(path), "--data-dir", str(tmp_path / "output")]
    result = run(arguments)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "READY_FOR_REVIEW"
    blocked = submission.model_copy(update={"continuity_file": None})
    path.write_text(blocked.model_dump_json(), encoding="utf-8")
    result = run(arguments)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["status"] == "BLOCKED"
    path.write_text('{"sensitive-field":"never-echo-this"}', encoding="utf-8")
    result = run(arguments)
    assert result.returncode == 2, result.stderr
    output = result.stdout
    assert "never-echo-this" not in output
    assert json.loads(output)["status"] == "INVALID_SUBMISSION"
    result = run(["--schema"])
    assert result.returncode == 0, result.stderr
    assert "candidate" in json.loads(result.stdout)["properties"]
