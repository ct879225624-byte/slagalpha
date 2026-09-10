"""Paid-source qualification stays content-addressed and cannot authorize research."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.data.historical_rule_sources import (
    HistoricalRuleSourceError,
    QualificationDocument,
    QualificationFinding,
    QualificationFindingState,
    RuleSourceCriterion,
    RuleSourceQualificationReport,
    RuleSourceQualificationStatus,
    build_rule_source_qualification,
    write_rule_source_qualification,
)
from slagalpha.reporting.run_manifest import canonical_json_bytes

DOC_URL = "https://vendor.example/public-schema"


def _documents() -> tuple[QualificationDocument, ...]:
    return (QualificationDocument(title="Public schema", url=DOC_URL, purpose="SCHEMA"),)


def _findings(
    *,
    default: QualificationFindingState = QualificationFindingState.CONFIRMED,
    overrides: dict[RuleSourceCriterion, QualificationFindingState] | None = None,
) -> tuple[QualificationFinding, ...]:
    states = overrides or {}
    return tuple(
        QualificationFinding(
            criterion=criterion,
            state=states.get(criterion, default),
            evidence_urls=(DOC_URL,),
            note=f"Synthetic {criterion.value} finding.",
        )
        for criterion in sorted(RuleSourceCriterion, key=lambda item: item.value)
    )


def _report(
    *,
    default: QualificationFindingState = QualificationFindingState.CONFIRMED,
    overrides: dict[RuleSourceCriterion, QualificationFindingState] | None = None,
) -> RuleSourceQualificationReport:
    return build_rule_source_qualification(
        provider="Synthetic Vendor",
        product="Historical rules",
        assessed_on=date(2026, 9, 10),
        dev_start=date(2023, 8, 1),
        dev_end=date(2025, 1, 30),
        documents=_documents(),
        findings=_findings(default=default, overrides=overrides),
    )


def test_complete_guaranteed_source_is_qualified_but_does_not_authorize_research() -> None:
    report = _report()
    assert report.status is RuleSourceQualificationStatus.QUALIFIED
    assert report.blockers == ()
    assert report.registry_modified is report.research_authorized is False
    assert report.purchase_authorized is report.credentials_used is False
    assert report.paid_api_called is report.locked_test_consumed is False


def test_best_effort_or_undocumented_evidence_requires_vendor_confirmation() -> None:
    report = _report(overrides={
        RuleSourceCriterion.TICK_SIZE: QualificationFindingState.BEST_EFFORT,
        RuleSourceCriterion.MAX_QUANTITY: QualificationFindingState.NOT_DOCUMENTED,
    })
    assert report.status is RuleSourceQualificationStatus.VENDOR_CONFIRMATION_REQUIRED
    assert report.blockers == (
        "MAX_QUANTITY:NOT_DOCUMENTED",
        "TICK_SIZE:BEST_EFFORT",
    )


@pytest.mark.parametrize("criterion", [
    RuleSourceCriterion.DEV_DATE_COVERAGE,
    RuleSourceCriterion.AUDITABLE_LICENSE,
])
def test_insufficient_coverage_or_unauditable_license_rejects_source(
    criterion: RuleSourceCriterion,
) -> None:
    report = _report(overrides={
        criterion: QualificationFindingState.NOT_AVAILABLE,
    })
    assert report.status is RuleSourceQualificationStatus.REJECTED


def test_missing_criterion_unreviewed_url_and_hash_tampering_are_rejected() -> None:
    with pytest.raises(ValidationError, match="every criterion"):
        build_rule_source_qualification(
            provider="Synthetic Vendor",
            product="Historical rules",
            assessed_on=date(2026, 9, 10),
            dev_start=date(2023, 8, 1),
            dev_end=date(2025, 1, 30),
            documents=_documents(),
            findings=_findings()[:-1],
        )
    findings = list(_findings())
    findings[0] = findings[0].model_copy(update={
        "evidence_urls": ("https://unreviewed.example/schema",),
    })
    with pytest.raises(ValidationError, match="reviewed documents"):
        build_rule_source_qualification(
            provider="Synthetic Vendor",
            product="Historical rules",
            assessed_on=date(2026, 9, 10),
            dev_start=date(2023, 8, 1),
            dev_end=date(2025, 1, 30),
            documents=_documents(),
            findings=tuple(findings),
        )
    report = _report()
    with pytest.raises(ValidationError, match="content hash"):
        RuleSourceQualificationReport.model_validate({
            **report.model_dump(mode="json"), "report_hash": "f" * 64,
        })


def test_qualification_writer_is_immutable_and_idempotent(tmp_path: Path) -> None:
    report = _report(default=QualificationFindingState.NOT_DOCUMENTED)
    path = write_rule_source_qualification(report, tmp_path)
    assert RuleSourceQualificationReport.model_validate_json(path.read_bytes()) == report
    assert path.read_bytes() == canonical_json_bytes(report.model_dump(mode="json"))
    assert write_rule_source_qualification(report, tmp_path) == path
    path.write_bytes(b"tampered")
    with pytest.raises(HistoricalRuleSourceError, match="changed"):
        write_rule_source_qualification(report, tmp_path)


@pytest.mark.parametrize(("digest", "provider"), [
    ("edd15e1f58a70e989de25edeead722687426b6363ac9a0629200097cfa5984c5", "Tardis.dev"),
    ("ba2d2619122c4b5dd5c895177095889ef90ba9923e8168b945f6bb5a9aa801b6", "Kaiko"),
    ("d286a057144bbd17ae9205f0125b4c82c0184aa42a7623f1c635a2957a09c0c6", "Amberdata"),
])
def test_checked_in_paid_source_reports_never_authorize_research(
    digest: str,
    provider: str,
) -> None:
    path = Path("data/manifests/rule_source_qualification") / f"{digest}.json"
    report = RuleSourceQualificationReport.model_validate_json(path.read_bytes())
    assert report.provider == provider
    assert report.status is RuleSourceQualificationStatus.VENDOR_CONFIRMATION_REQUIRED
    assert report.registry_modified is report.research_authorized is False
