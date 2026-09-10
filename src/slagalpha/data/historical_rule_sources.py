"""Audit archived exchangeInfo observations without inferring rule intervals."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.exchange_info import (
    ExchangeContract,
    ExchangeInfoError,
    parse_exchange_info,
)
from slagalpha.reporting.run_manifest import (
    RunManifestError,
    _publish_immutable,
    canonical_json_bytes,
)


class HistoricalRuleSourceError(ValueError):
    """Malformed archive metadata, response, or immutable output."""


class RuleSourceCriterion(StrEnum):
    """Evidence required before a paid source can support historical rule intake."""

    AUDITABLE_LICENSE = "AUDITABLE_LICENSE"
    BINANCE_USDM_COVERAGE = "BINANCE_USDM_COVERAGE"
    DEV_DATE_COVERAGE = "DEV_DATE_COVERAGE"
    EXACT_EFFECTIVE_TIME = "EXACT_EFFECTIVE_TIME"
    HISTORICAL_CHANGE_COMPLETENESS = "HISTORICAL_CHANGE_COMPLETENESS"
    MAX_QUANTITY = "MAX_QUANTITY"
    MIN_NOTIONAL = "MIN_NOTIONAL"
    MIN_QUANTITY = "MIN_QUANTITY"
    ORIGINAL_SOURCE_TIMESTAMP = "ORIGINAL_SOURCE_TIMESTAMP"
    RAW_BYTES_EXPORT = "RAW_BYTES_EXPORT"
    STEP_SIZE = "STEP_SIZE"
    TICK_SIZE = "TICK_SIZE"


class QualificationFindingState(StrEnum):
    """Strength of the public evidence for one qualification criterion."""

    BEST_EFFORT = "BEST_EFFORT"
    CONFIRMED = "CONFIRMED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    NOT_DOCUMENTED = "NOT_DOCUMENTED"


class RuleSourceQualificationStatus(StrEnum):
    """Only QUALIFIED may proceed to a local evidence pilot."""

    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    VENDOR_CONFIRMATION_REQUIRED = "VENDOR_CONFIRMATION_REQUIRED"


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("historical rule source timestamps must use UTC")
    return value


def _sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("historical rule source hashes must be lowercase SHA-256")
    return normalized


class QualificationDocument(BaseModel):
    """One public vendor document reviewed without account or API access."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    url: str
    purpose: Literal["COVERAGE", "LICENSE", "SCHEMA", "SUBSCRIPTION"]

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("qualification document title must not be blank")
        return normalized

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("https://") or any(c.isspace() for c in normalized):
            raise ValueError("qualification documents must use public HTTPS URLs")
        return normalized


class QualificationFinding(BaseModel):
    """Public-document finding for one mandatory source capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: RuleSourceCriterion
    state: QualificationFindingState
    evidence_urls: tuple[str, ...] = Field(min_length=1)
    note: str

    @field_validator("evidence_urls")
    @classmethod
    def validate_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))):
            raise ValueError("qualification evidence URLs must be unique and canonical")
        if any(not value.startswith("https://") or any(c.isspace() for c in value)
               for value in values):
            raise ValueError("qualification evidence must use public HTTPS URLs")
        return values

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("qualification finding note must not be blank")
        return normalized


class RuleSourceQualificationReport(BaseModel):
    """Fail-closed paid-source screening; never modifies rule or research state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["rule-source-qualification/0.1.0"] = (
        "rule-source-qualification/0.1.0"
    )
    provider: str
    product: str
    assessed_on: date
    dev_start: date
    dev_end: date
    documents: tuple[QualificationDocument, ...] = Field(min_length=1)
    findings: tuple[QualificationFinding, ...]
    status: RuleSourceQualificationStatus
    blockers: tuple[str, ...]
    public_documentation_only: Literal[True] = True
    purchase_authorized: Literal[False] = False
    credentials_used: Literal[False] = False
    paid_api_called: Literal[False] = False
    registry_modified: Literal[False] = False
    research_authorized: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    report_hash: str

    @field_validator("provider", "product")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("qualification provider and product must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.dev_end < self.dev_start:
            raise ValueError("qualification DEV interval is inverted")
        if self.documents != tuple(sorted(self.documents, key=lambda item: item.url)):
            raise ValueError("qualification documents must be sorted by URL")
        document_urls = tuple(document.url for document in self.documents)
        if len(set(document_urls)) != len(document_urls):
            raise ValueError("qualification documents must be unique")
        expected_criteria = tuple(sorted(RuleSourceCriterion, key=lambda item: item.value))
        observed_criteria = tuple(finding.criterion for finding in self.findings)
        if observed_criteria != expected_criteria:
            raise ValueError("qualification findings must contain every criterion canonically")
        if any(url not in document_urls for finding in self.findings
               for url in finding.evidence_urls):
            raise ValueError("qualification findings must reference reviewed documents")

        states = tuple(finding.state for finding in self.findings)
        if QualificationFindingState.NOT_AVAILABLE in states:
            expected_status = RuleSourceQualificationStatus.REJECTED
        elif all(state is QualificationFindingState.CONFIRMED for state in states):
            expected_status = RuleSourceQualificationStatus.QUALIFIED
        else:
            expected_status = RuleSourceQualificationStatus.VENDOR_CONFIRMATION_REQUIRED
        if self.status is not expected_status:
            raise ValueError("qualification status does not match findings")
        expected_blockers = tuple(
            f"{finding.criterion.value}:{finding.state.value}"
            for finding in self.findings
            if finding.state is not QualificationFindingState.CONFIRMED
        )
        if self.blockers != expected_blockers:
            raise ValueError("qualification blockers do not match findings")
        if self.report_hash != _hash(self.model_dump(mode="json", exclude={"report_hash"})):
            raise ValueError("rule source qualification content hash mismatch")
        return self


def build_rule_source_qualification(
    *,
    provider: str,
    product: str,
    assessed_on: date,
    dev_start: date,
    dev_end: date,
    documents: tuple[QualificationDocument, ...],
    findings: tuple[QualificationFinding, ...],
) -> RuleSourceQualificationReport:
    """Derive the only permitted status from a complete public-document review."""

    ordered_documents = tuple(sorted(documents, key=lambda item: item.url))
    ordered_findings = tuple(sorted(findings, key=lambda item: item.criterion.value))
    states = tuple(finding.state for finding in ordered_findings)
    if QualificationFindingState.NOT_AVAILABLE in states:
        status = RuleSourceQualificationStatus.REJECTED
    elif states and all(state is QualificationFindingState.CONFIRMED for state in states):
        status = RuleSourceQualificationStatus.QUALIFIED
    else:
        status = RuleSourceQualificationStatus.VENDOR_CONFIRMATION_REQUIRED
    payload = {
        "schema_version": "rule-source-qualification/0.1.0",
        "provider": provider,
        "product": product,
        "assessed_on": assessed_on.isoformat(),
        "dev_start": dev_start.isoformat(),
        "dev_end": dev_end.isoformat(),
        "documents": [item.model_dump(mode="json") for item in ordered_documents],
        "findings": [item.model_dump(mode="json") for item in ordered_findings],
        "status": status.value,
        "blockers": [
            f"{finding.criterion.value}:{finding.state.value}"
            for finding in ordered_findings
            if finding.state is not QualificationFindingState.CONFIRMED
        ],
        "public_documentation_only": True,
        "purchase_authorized": False,
        "credentials_used": False,
        "paid_api_called": False,
        "registry_modified": False,
        "research_authorized": False,
        "locked_test_consumed": False,
    }
    return RuleSourceQualificationReport.model_validate(
        {**payload, "report_hash": _hash(payload)}
    )


def write_rule_source_qualification(
    report: RuleSourceQualificationReport, data_dir: Path
) -> Path:
    """Write one immutable content-addressed provider report."""

    report = RuleSourceQualificationReport.model_validate(report.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "rule_source_qualification" / f"{report.report_hash}.json"
    )
    content = canonical_json_bytes(report.model_dump(mode="json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _publish_immutable(destination, content)
    except RunManifestError as error:
        raise HistoricalRuleSourceError(
            f"existing rule source qualification changed: {destination}"
        ) from error
    return destination


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HistoricalRuleSourceError("duplicate JSON key in archived exchangeInfo")
        result[key] = value
    return result


class ArchivedExchangeInfoSource(BaseModel):
    """Immutable provenance for a third-party replay of an official public response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive: Literal["INTERNET_ARCHIVE_WAYBACK"] = "INTERNET_ARCHIVE_WAYBACK"
    original_url: Literal["https://fapi.binance.com/fapi/v1/exchangeInfo"] = (
        "https://fapi.binance.com/fapi/v1/exchangeInfo"
    )
    replay_url: str
    archive_capture_at: datetime
    retrieved_at: datetime
    cdx_digest: str
    raw_sha256: str

    @field_validator("archive_capture_at", "retrieved_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("replay_url", "cdx_digest")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("archive provenance values must not be blank")
        return normalized

    @field_validator("raw_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.retrieved_at < self.archive_capture_at:
            raise ValueError("retrieved_at must not precede archive_capture_at")
        return self


class HistoricalRuleSourceAudit(BaseModel):
    """Point-in-time source inventory; never grants interval rule coverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["historical-rule-source-audit/0.1.0"] = (
        "historical-rule-source-audit/0.1.0"
    )
    dev_rule_gap_hash: str
    source: ArchivedExchangeInfoSource
    exchange_server_time: datetime
    capture_lag_ms: int = Field(ge=0)
    snapshot_symbol_count: int = Field(ge=0)
    target_symbol_count: int = Field(ge=0)
    observed_target_symbols: tuple[str, ...]
    missing_target_symbols: tuple[str, ...]
    unparseable_snapshot_symbols: tuple[str, ...]
    priority_observations: tuple[ExchangeContract, ...]
    eligible_member_day_count: Literal[0] = 0
    status: Literal["BLOCKED"] = "BLOCKED"
    blockers: tuple[str, ...]
    registry_modified: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    report_hash: str

    @field_validator("dev_rule_gap_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("exchange_server_time")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.capture_lag_ms != int(
            (self.source.archive_capture_at - self.exchange_server_time).total_seconds() * 1000
        ):
            raise ValueError("capture lag does not match source and exchange timestamps")
        if self.target_symbol_count != len(self.observed_target_symbols) + len(
            self.missing_target_symbols
        ):
            raise ValueError("target symbol counts do not reconcile")
        for values in (
            self.observed_target_symbols,
            self.missing_target_symbols,
            self.unparseable_snapshot_symbols,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("historical source symbol lists must be unique and canonical")
        priority_symbols = tuple(observation.symbol for observation in self.priority_observations)
        if len(set(priority_symbols)) != len(priority_symbols):
            raise ValueError("priority observations must be unique")
        if any(symbol not in self.observed_target_symbols for symbol in priority_symbols):
            raise ValueError("priority observations must be observed DEV targets")
        if self.blockers != tuple(sorted(set(self.blockers))) or not self.blockers:
            raise ValueError("historical source blockers must be non-empty and canonical")
        expected = _hash(self.model_dump(mode="json", exclude={"report_hash"}))
        if self.report_hash != expected:
            raise ValueError("historical rule source audit content hash mismatch")
        return self


def _parse_one(
    raw_contract: dict[str, Any], server_time: Any, fetched_at: datetime
) -> ExchangeContract:
    reduced = json.dumps(
        {"serverTime": server_time, "symbols": [raw_contract]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return parse_exchange_info(reduced, fetched_at=fetched_at).contracts[0]


def audit_archived_exchange_info(
    raw: bytes,
    *,
    source: ArchivedExchangeInfoSource,
    dev_rule_gap_hash: str,
    target_symbols: tuple[str, ...],
    priority_symbols: tuple[str, ...],
) -> HistoricalRuleSourceAudit:
    """Extract exact observations while explicitly retaining the continuity blocker."""

    if hashlib.sha256(raw).hexdigest() != source.raw_sha256:
        raise HistoricalRuleSourceError("archived exchangeInfo raw hash changed")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoricalRuleSourceError("archived exchangeInfo is not valid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise HistoricalRuleSourceError("archived exchangeInfo root or symbols is invalid")
    raw_contracts = payload["symbols"]
    if any(not isinstance(item, dict) for item in raw_contracts):
        raise HistoricalRuleSourceError("archived exchangeInfo symbol entry is invalid")

    parsed: dict[str, ExchangeContract] = {}
    unparseable: list[str] = []
    for item in raw_contracts:
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise HistoricalRuleSourceError("archived exchangeInfo symbol is invalid")
        if symbol in parsed or symbol in unparseable:
            raise HistoricalRuleSourceError("archived exchangeInfo symbols are not unique")
        try:
            parsed[symbol] = _parse_one(item, payload.get("serverTime"), source.archive_capture_at)
        except (ExchangeInfoError, ValueError, TypeError):
            unparseable.append(symbol)

    targets = tuple(sorted(set(target_symbols)))
    if not targets or len(targets) != len(target_symbols):
        raise HistoricalRuleSourceError("target symbols must be non-empty and unique")
    if len(set(priority_symbols)) != len(priority_symbols) or any(
        symbol not in targets for symbol in priority_symbols
    ):
        raise HistoricalRuleSourceError("priority symbols must be unique DEV targets")
    observed = tuple(symbol for symbol in targets if symbol in parsed)
    missing = tuple(symbol for symbol in targets if symbol not in parsed)
    priority = tuple(parsed[symbol] for symbol in priority_symbols if symbol in parsed)
    try:
        server_time = parse_exchange_info(
            json.dumps({"serverTime": payload.get("serverTime"), "symbols": []}).encode(),
            fetched_at=source.archive_capture_at,
        ).server_time
    except (ExchangeInfoError, ValueError, TypeError) as error:
        raise HistoricalRuleSourceError("archived exchangeInfo serverTime is invalid") from error
    if server_time > source.archive_capture_at:
        raise HistoricalRuleSourceError("exchange serverTime follows archive capture time")

    blockers = {
        "ARCHIVE_SOURCE_AUTHENTICITY_REVIEW_REQUIRED",
        "POINT_IN_TIME_SNAPSHOT_HAS_NO_INTERVAL_CONTINUITY",
    }
    if missing:
        blockers.add("DEV_TARGETS_MISSING_FROM_SNAPSHOT")
    if unparseable:
        blockers.add("SNAPSHOT_CONTAINS_UNPARSEABLE_CONTRACTS")
    payload_out = {
        "schema_version": "historical-rule-source-audit/0.1.0",
        "dev_rule_gap_hash": dev_rule_gap_hash,
        "source": source.model_dump(mode="json"),
        "exchange_server_time": server_time.isoformat().replace("+00:00", "Z"),
        "capture_lag_ms": int((source.archive_capture_at - server_time).total_seconds() * 1000),
        "snapshot_symbol_count": len(raw_contracts),
        "target_symbol_count": len(targets),
        "observed_target_symbols": list(observed),
        "missing_target_symbols": list(missing),
        "unparseable_snapshot_symbols": sorted(unparseable),
        "priority_observations": [item.model_dump(mode="json") for item in priority],
        "eligible_member_day_count": 0,
        "status": "BLOCKED",
        "blockers": sorted(blockers),
        "registry_modified": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    return HistoricalRuleSourceAudit.model_validate(
        {**payload_out, "report_hash": _hash(payload_out)}
    )


def write_historical_rule_source_audit(
    report: HistoricalRuleSourceAudit, data_dir: Path
) -> Path:
    report = HistoricalRuleSourceAudit.model_validate(report.model_dump(mode="json"))
    destination = (
        data_dir / "manifests" / "historical_rule_source_audit" / f"{report.report_hash}.json"
    )
    content = (json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise HistoricalRuleSourceError(
                f"existing historical source audit changed: {destination}"
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
