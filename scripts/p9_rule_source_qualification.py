"""Qualify paid historical-rule sources from public documents only."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from slagalpha.data.historical_rule_sources import (
    QualificationDocument,
    QualificationFinding,
    QualificationFindingState,
    RuleSourceCriterion,
    RuleSourceQualificationReport,
    build_rule_source_qualification,
    write_rule_source_qualification,
)

ROOT = Path(__file__).resolve().parents[1]
ASSESSED_ON = date(2026, 9, 10)
DEV_START = date(2023, 8, 1)
DEV_END = date(2025, 1, 30)


def _findings(
    states: Mapping[RuleSourceCriterion, QualificationFindingState],
    evidence: Mapping[RuleSourceCriterion, tuple[str, ...]],
    notes: Mapping[RuleSourceCriterion, str],
) -> tuple[QualificationFinding, ...]:
    return tuple(
        QualificationFinding(
            criterion=criterion,
            state=states[criterion],
            evidence_urls=tuple(sorted(evidence[criterion])),
            note=notes[criterion],
        )
        for criterion in sorted(RuleSourceCriterion, key=lambda item: item.value)
    )


def _tardis() -> RuleSourceQualificationReport:
    metadata = "https://docs.tardis.dev/api/instruments-metadata-api"
    coverage = "https://docs.tardis.dev/historical-data-details/binance-futures"
    billing = "https://docs.tardis.dev/faq/billing-and-subscriptions"
    terms = "https://docs.tardis.dev/legal/terms-of-service"
    documents = (
        QualificationDocument(title="Instruments Metadata API", url=metadata, purpose="SCHEMA"),
        QualificationDocument(
            title="Binance USDS-M Futures historical data", url=coverage, purpose="COVERAGE"
        ),
        QualificationDocument(
            title="Billing and Subscriptions", url=billing, purpose="SUBSCRIPTION"
        ),
        QualificationDocument(title="Terms of Service", url=terms, purpose="LICENSE"),
    )
    states = {
        RuleSourceCriterion.AUDITABLE_LICENSE: QualificationFindingState.CONFIRMED,
        RuleSourceCriterion.BINANCE_USDM_COVERAGE: QualificationFindingState.CONFIRMED,
        RuleSourceCriterion.DEV_DATE_COVERAGE: QualificationFindingState.CONFIRMED,
        RuleSourceCriterion.EXACT_EFFECTIVE_TIME: QualificationFindingState.BEST_EFFORT,
        RuleSourceCriterion.HISTORICAL_CHANGE_COMPLETENESS: (
            QualificationFindingState.BEST_EFFORT
        ),
        RuleSourceCriterion.MAX_QUANTITY: QualificationFindingState.NOT_DOCUMENTED,
        RuleSourceCriterion.MIN_NOTIONAL: QualificationFindingState.BEST_EFFORT,
        RuleSourceCriterion.MIN_QUANTITY: QualificationFindingState.BEST_EFFORT,
        RuleSourceCriterion.ORIGINAL_SOURCE_TIMESTAMP: (
            QualificationFindingState.NOT_DOCUMENTED
        ),
        RuleSourceCriterion.RAW_BYTES_EXPORT: QualificationFindingState.NOT_DOCUMENTED,
        RuleSourceCriterion.STEP_SIZE: QualificationFindingState.BEST_EFFORT,
        RuleSourceCriterion.TICK_SIZE: QualificationFindingState.BEST_EFFORT,
    }
    evidence = {
        RuleSourceCriterion.AUDITABLE_LICENSE: (terms,),
        RuleSourceCriterion.BINANCE_USDM_COVERAGE: (coverage,),
        RuleSourceCriterion.DEV_DATE_COVERAGE: (billing, coverage),
        RuleSourceCriterion.EXACT_EFFECTIVE_TIME: (metadata,),
        RuleSourceCriterion.HISTORICAL_CHANGE_COMPLETENESS: (metadata,),
        RuleSourceCriterion.MAX_QUANTITY: (metadata,),
        RuleSourceCriterion.MIN_NOTIONAL: (metadata,),
        RuleSourceCriterion.MIN_QUANTITY: (metadata,),
        RuleSourceCriterion.ORIGINAL_SOURCE_TIMESTAMP: (metadata,),
        RuleSourceCriterion.RAW_BYTES_EXPORT: (coverage, metadata),
        RuleSourceCriterion.STEP_SIZE: (metadata,),
        RuleSourceCriterion.TICK_SIZE: (metadata,),
    }
    notes = {
        RuleSourceCriterion.AUDITABLE_LICENSE: (
            "Public terms allow internal business use and storage; any acquired plan must retain "
            "those terms with the exported evidence."
        ),
        RuleSourceCriterion.BINANCE_USDM_COVERAGE: (
            "The vendor has a dedicated Binance USDS-M Futures dataset."
        ),
        RuleSourceCriterion.DEV_DATE_COVERAGE: (
            "The dataset starts in 2019, but purchasable historical range depends on subscription "
            "type and billing period."
        ),
        RuleSourceCriterion.EXACT_EFFECTIVE_TIME: (
            "Changes have an ISO until value, but non-multiplier change tracking is best-effort."
        ),
        RuleSourceCriterion.HISTORICAL_CHANGE_COMPLETENESS: (
            "Only contractMultiplier history is guaranteed accurate and complete; other changes "
            "are explicitly best-effort and may be incomplete."
        ),
        RuleSourceCriterion.MAX_QUANTITY: (
            "The public instrument schema does not document a maximum trade quantity field."
        ),
        RuleSourceCriterion.MIN_NOTIONAL: (
            "Current metadata includes minNotional, but complete historical changes are not "
            "guaranteed."
        ),
        RuleSourceCriterion.MIN_QUANTITY: (
            "Current metadata includes minTradeAmount, but complete historical changes are not "
            "guaranteed."
        ),
        RuleSourceCriterion.ORIGINAL_SOURCE_TIMESTAMP: (
            "The normalized metadata history does not document an exchange-origin timestamp for "
            "each rule observation."
        ),
        RuleSourceCriterion.RAW_BYTES_EXPORT: (
            "Raw market replay is documented, but raw exchangeInfo bytes tied to every metadata "
            "change are not."
        ),
        RuleSourceCriterion.STEP_SIZE: (
            "amountIncrement history is explicitly tracked on a best-effort basis."
        ),
        RuleSourceCriterion.TICK_SIZE: (
            "priceIncrement history is explicitly tracked on a best-effort basis."
        ),
    }
    return build_rule_source_qualification(
        provider="Tardis.dev",
        product="Instruments Metadata API and raw replay",
        assessed_on=ASSESSED_ON,
        dev_start=DEV_START,
        dev_end=DEV_END,
        documents=documents,
        findings=_findings(states, evidence, notes),
    )


def _kaiko() -> RuleSourceQualificationReport:
    instruments = (
        "https://docs.kaiko.com/rest-api/data-feeds/reference-data/basic-tier/"
        "exchange-trading-pair-codes-instruments"
    )
    dictionary = "https://docs.kaiko.com/explore-our-data/data-dictionary"
    versioning = "https://docs.kaiko.com/rest-api/general/getting-started/data-versioning"
    licensing = "https://www.kaiko.com/about-kaiko/pricing-and-contracts"
    documents = (
        QualificationDocument(title="Data dictionary", url=dictionary, purpose="COVERAGE"),
        QualificationDocument(title="Data versioning", url=versioning, purpose="SCHEMA"),
        QualificationDocument(title="Instrument reference data", url=instruments, purpose="SCHEMA"),
        QualificationDocument(title="Pricing and licensing", url=licensing, purpose="LICENSE"),
    )
    states = {
        criterion: QualificationFindingState.NOT_DOCUMENTED
        for criterion in RuleSourceCriterion
    }
    evidence: dict[RuleSourceCriterion, tuple[str, ...]] = {
        criterion: (instruments,) for criterion in RuleSourceCriterion
    }
    evidence[RuleSourceCriterion.DEV_DATE_COVERAGE] = (dictionary, instruments)
    evidence[RuleSourceCriterion.HISTORICAL_CHANGE_COMPLETENESS] = (versioning,)
    evidence[RuleSourceCriterion.RAW_BYTES_EXPORT] = (dictionary,)
    evidence[RuleSourceCriterion.AUDITABLE_LICENSE] = (licensing,)
    notes = {
        criterion: (
            "The reviewed public instrument schema identifies instruments and trade availability "
            "but does not document this historical rule criterion."
        )
        for criterion in RuleSourceCriterion
    }
    notes[RuleSourceCriterion.DEV_DATE_COVERAGE] = (
        "Historical market-data coverage is described, but no historical contract-rule coverage "
        "for the DEV dates is documented."
    )
    notes[RuleSourceCriterion.HISTORICAL_CHANGE_COMPLETENESS] = (
        "Dataset versioning is documented for market data, not a complete rule-change history."
    )
    notes[RuleSourceCriterion.RAW_BYTES_EXPORT] = (
        "CSV delivery is documented for market data, not exchange-native contract-rule snapshots."
    )
    notes[RuleSourceCriterion.AUDITABLE_LICENSE] = (
        "Plans and usage are contract-specific; the reviewed page does not grant the required "
        "retention and audit rights for this evidence workflow."
    )
    return build_rule_source_qualification(
        provider="Kaiko",
        product="Reference Data and historical market data",
        assessed_on=ASSESSED_ON,
        dev_start=DEV_START,
        dev_end=DEV_END,
        documents=documents,
        findings=_findings(states, evidence, notes),
    )


def _amberdata() -> RuleSourceQualificationReport:
    reference = "https://docs.amberdata.io/http/market/futures-exchanges-reference"
    coverage = "https://docs.amberdata.io/http/market/futures-exchanges-information"
    licensing = "https://www.amberdata.io/online-market-data-ordering-faq"
    documents = (
        QualificationDocument(title="Futures reference", url=reference, purpose="SCHEMA"),
        QualificationDocument(title="Futures instruments", url=coverage, purpose="COVERAGE"),
        QualificationDocument(title="Market data ordering FAQ", url=licensing, purpose="LICENSE"),
    )
    states = {
        criterion: QualificationFindingState.NOT_DOCUMENTED
        for criterion in RuleSourceCriterion
    }
    states[RuleSourceCriterion.MIN_QUANTITY] = QualificationFindingState.CONFIRMED
    states[RuleSourceCriterion.MAX_QUANTITY] = QualificationFindingState.CONFIRMED
    evidence: dict[RuleSourceCriterion, tuple[str, ...]] = {
        criterion: (reference,) for criterion in RuleSourceCriterion
    }
    evidence[RuleSourceCriterion.DEV_DATE_COVERAGE] = (coverage, reference)
    evidence[RuleSourceCriterion.RAW_BYTES_EXPORT] = (coverage, licensing, reference)
    evidence[RuleSourceCriterion.AUDITABLE_LICENSE] = (licensing,)
    notes = {
        criterion: (
            "The reviewed public current-reference schema does not document this criterion as a "
            "complete historical series."
        )
        for criterion in RuleSourceCriterion
    }
    notes[RuleSourceCriterion.BINANCE_USDM_COVERAGE] = (
        "The schema has Binance futures examples but does not identify Binance USDS-M as a "
        "separate guaranteed segment."
    )
    notes[RuleSourceCriterion.DEV_DATE_COVERAGE] = (
        "Per-instrument market-data timelines do not establish historical contract-rule coverage "
        "for the DEV interval."
    )
    notes[RuleSourceCriterion.MIN_QUANTITY] = (
        "The current futures reference schema includes limitsVolumeMin."
    )
    notes[RuleSourceCriterion.MAX_QUANTITY] = (
        "The current futures reference schema includes limitsVolumeMax."
    )
    notes[RuleSourceCriterion.TICK_SIZE] = (
        "precisionPrice is documented, but equivalence to every historical Binance PRICE_FILTER "
        "tickSize is not guaranteed."
    )
    notes[RuleSourceCriterion.STEP_SIZE] = (
        "precisionVolume is documented, but equivalence to every historical Binance LOT_SIZE "
        "stepSize is not guaranteed."
    )
    notes[RuleSourceCriterion.MIN_NOTIONAL] = (
        "limitsCostMin exists in the schema but is null in the public Binance futures example."
    )
    notes[RuleSourceCriterion.RAW_BYTES_EXPORT] = (
        "Bulk raw market datasets are offered, but raw historical rule snapshots are not "
        "documented."
    )
    notes[RuleSourceCriterion.AUDITABLE_LICENSE] = (
        "The standard license allows commercial use and forbids redistribution; explicit archival "
        "and audit terms for rule evidence still require confirmation."
    )
    return build_rule_source_qualification(
        provider="Amberdata",
        product="Futures Exchange Reference",
        assessed_on=ASSESSED_ON,
        dev_start=DEV_START,
        dev_end=DEV_END,
        documents=documents,
        findings=_findings(states, evidence, notes),
    )


def main() -> int:
    reports = (_tardis(), _kaiko(), _amberdata())
    outputs = []
    for report in reports:
        path = write_rule_source_qualification(report, ROOT / "data")
        outputs.append({
            "provider": report.provider,
            "report_hash": report.report_hash,
            "report_path": str(path.relative_to(ROOT)),
            "status": report.status,
        })
    print(json.dumps({"reports": outputs, "research_authorized": False}, sort_keys=True))
    return 0 if all(item["status"] == "QUALIFIED" for item in outputs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
