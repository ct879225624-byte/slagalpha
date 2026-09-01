"""Local historical rule evidence intake; never promotes or edits a registry."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from slagalpha.data.exchange_info import ExchangeInfoError, parse_exchange_info
from slagalpha.domain.universe import ContractRegistryEntry, RegistryVerification


class RuleEvidenceError(ValueError):
    """Malformed submission or immutable evidence conflict."""


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class RuleEvidenceSubmission(BaseModel):
    """A claimed closed interval and its locally available, still-unreviewed evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["rule-evidence-submission/0.1.0"] = "rule-evidence-submission/0.1.0"
    candidate: ContractRegistryEntry
    snapshot_file: str
    snapshot_observed_at: datetime
    collected_at: datetime
    continuity_file: str | None = None
    continuity_sha256: str | None = None
    continuity_note: str | None = None

    @field_validator("snapshot_observed_at", "collected_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("evidence timestamps must use UTC")
        return value

    @field_validator("continuity_sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("continuity_sha256 must be lowercase SHA-256")
        return value

    @field_validator("continuity_note")
    @classmethod
    def validate_note(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("continuity_note must not be blank")
        return None if value is None else value.strip()

    @model_validator(mode="after")
    def validate_submission(self) -> Self:
        if self.candidate.verification_status is not RegistryVerification.UNVERIFIED:
            raise ValueError("intake accepts UNVERIFIED candidates only")
        if self.collected_at < self.snapshot_observed_at:
            raise ValueError("collected_at must not precede snapshot_observed_at")
        return self


_MANUAL_REVIEW_CHECKS = (
    "SOURCE_AUTHENTICITY",
    "HISTORICAL_CAPTURE_TIMESTAMP",
    "INTERVAL_CONTINUITY",
    "IDENTITY_LIFECYCLE_EVIDENCE",
    "REGISTRY_INTERVAL_CONFLICTS",
)


class RuleEvidenceIntakeReport(BaseModel):
    """Only document completeness/consistency; READY_FOR_REVIEW is not VERIFIED."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["rule-evidence-intake/0.1.0"] = "rule-evidence-intake/0.1.0"
    submission_hash: str
    symbol: str
    snapshot_actual_sha256: str | None
    continuity_actual_sha256: str | None
    status: Literal["BLOCKED", "READY_FOR_REVIEW"]
    blockers: tuple[str, ...]
    mismatched_fields: tuple[str, ...]
    manual_review_checks: tuple[str, ...] = _MANUAL_REVIEW_CHECKS
    verification_status: Literal[RegistryVerification.UNVERIFIED] = RegistryVerification.UNVERIFIED
    registry_modified: Literal[False] = False
    research_authorized: Literal[False] = False
    report_hash: str

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if (self.status == "BLOCKED") != bool(self.blockers):
            raise ValueError("intake status and blockers disagree")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("intake blockers must be unique and canonical")
        if self.mismatched_fields != tuple(sorted(set(self.mismatched_fields))):
            raise ValueError("mismatched fields must be unique and canonical")
        if bool(self.mismatched_fields) != ("SNAPSHOT_METADATA_MISMATCH" in self.blockers):
            raise ValueError("metadata mismatch details and blockers disagree")
        if self.manual_review_checks != _MANUAL_REVIEW_CHECKS:
            raise ValueError("manual evidence review cannot be skipped")
        for value in (self.submission_hash, self.snapshot_actual_sha256,
                      self.continuity_actual_sha256):
            if value is not None and (
                len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("intake evidence hashes must be lowercase SHA-256")
        if self.report_hash != _hash(self.model_dump(mode="json", exclude={"report_hash"})):
            raise ValueError("intake content hash mismatch")
        return self


def _read_local_evidence(root: Path, name: str, kind: str) -> tuple[bytes | None, str | None]:
    """Reject absolute/traversal paths, Windows alternate streams and escaping symlinks."""

    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or "\\" in name or ":" in name
            or any(part in (".", "..", "") for part in name.split("/"))):
        return None, f"{kind}_PATH_UNSAFE"
    try:
        base = root.resolve(strict=True)
        path = base.joinpath(*relative.parts).resolve()
        if not path.is_relative_to(base):
            return None, f"{kind}_PATH_UNSAFE"
        if not path.is_file():
            return None, f"{kind}_FILE_MISSING"
        return path.read_bytes(), None
    except (OSError, RuntimeError, ValueError):
        return None, f"{kind}_FILE_UNREADABLE"


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuleEvidenceError("duplicate JSON key in evidence")
        result[key] = value
    return result


def load_rule_evidence_submission(raw: bytes) -> RuleEvidenceSubmission:
    """Reject duplicate keys in submissions as well as in raw exchange snapshots."""

    return RuleEvidenceSubmission.model_validate(
        json.loads(raw, object_pairs_hook=_unique_json_object)
    )


def audit_rule_evidence(
    submission: RuleEvidenceSubmission,
    *,
    evidence_root: Path,
) -> RuleEvidenceIntakeReport:
    """Read only declared local files; do not fetch URLs or infer an interval from a snapshot."""

    submission = RuleEvidenceSubmission.model_validate(submission.model_dump(mode="json"))
    candidate = submission.candidate
    blockers: list[str] = []
    mismatches: list[str] = []
    if candidate.effective_to is None:
        blockers.append("CLOSED_INTERVAL_REQUIRED")
    if (submission.snapshot_observed_at < candidate.effective_from
            or (candidate.effective_to is not None
                and submission.snapshot_observed_at >= candidate.effective_to)):
        blockers.append("SNAPSHOT_OUTSIDE_CLAIMED_INTERVAL")

    raw, error = _read_local_evidence(evidence_root, submission.snapshot_file, "SNAPSHOT")
    actual_hash = None if raw is None else hashlib.sha256(raw).hexdigest()
    if error is not None:
        blockers.append(error)
    elif actual_hash != candidate.source_snapshot_hash:
        blockers.append("SNAPSHOT_HASH_MISMATCH")
    else:
        assert raw is not None
        try:
            # The established parser is reused, but ambiguous raw JSON is rejected first.
            json.loads(raw, object_pairs_hook=_unique_json_object)
            snapshot = parse_exchange_info(raw, fetched_at=submission.snapshot_observed_at)
        except (ExchangeInfoError, ValueError, UnicodeDecodeError):
            blockers.append("SNAPSHOT_INVALID")
        else:
            contract = next((c for c in snapshot.contracts if c.symbol == candidate.symbol), None)
            if contract is None:
                blockers.append("SNAPSHOT_SYMBOL_MISSING")
            else:
                fields = (
                    "base_asset", "quote_asset", "margin_asset", "contract_type", "status",
                    "onboard_date", "tick_size", "step_size", "min_qty", "max_qty", "min_notional",
                )
                mismatches = sorted(
                    name for name in fields if getattr(contract, name) != getattr(candidate, name)
                )
                if mismatches:
                    blockers.append("SNAPSHOT_METADATA_MISMATCH")

    continuity_hash = None
    if (submission.continuity_file is None or submission.continuity_sha256 is None
            or submission.continuity_note is None):
        blockers.append("CONTINUITY_EVIDENCE_REQUIRED")
    else:
        continuity, error = _read_local_evidence(
            evidence_root, submission.continuity_file, "CONTINUITY"
        )
        if error is not None:
            blockers.append(error)
        elif continuity is not None:
            continuity_hash = hashlib.sha256(continuity).hexdigest()
            if continuity_hash != submission.continuity_sha256:
                blockers.append("CONTINUITY_HASH_MISMATCH")
            if not continuity.strip():
                blockers.append("CONTINUITY_EVIDENCE_EMPTY")
            if continuity_hash == actual_hash:
                blockers.append("SNAPSHOT_ALONE_IS_NOT_CONTINUITY_EVIDENCE")
    payload = {
        "schema_version": "rule-evidence-intake/0.1.0",
        "submission_hash": _hash(submission.model_dump(mode="json")),
        "symbol": candidate.symbol,
        "snapshot_actual_sha256": actual_hash,
        "continuity_actual_sha256": continuity_hash,
        "status": "BLOCKED" if blockers else "READY_FOR_REVIEW",
        "blockers": sorted(set(blockers)),
        "mismatched_fields": mismatches,
        "manual_review_checks": list(_MANUAL_REVIEW_CHECKS),
        "verification_status": RegistryVerification.UNVERIFIED.value,
        "registry_modified": False,
        "research_authorized": False,
    }
    return RuleEvidenceIntakeReport.model_validate({**payload, "report_hash": _hash(payload)})


def write_rule_evidence_report(report: RuleEvidenceIntakeReport, data_dir: Path) -> Path:
    report = RuleEvidenceIntakeReport.model_validate(report.model_dump(mode="json"))
    destination = data_dir / "manifests" / "rule_evidence_intake" / f"{report.report_hash}.json"
    content = (json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise RuleEvidenceError(f"existing evidence intake report changed: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
