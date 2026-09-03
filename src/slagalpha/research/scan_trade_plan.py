"""Bind recomputed P5 and P6 to historical rules and finite DEV data requests, never orders."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from slagalpha.domain.universe import (
    ContractRegistry,
    ContractRegistryEntry,
    RegistryVerification,
    UniverseSnapshot,
)
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.replay_inputs import (
    DevReplayDataRequest,
    Sha256,
    build_dev_replay_data_request,
)
from slagalpha.research.scan_features import revalidate_scan_features
from slagalpha.research.scan_plan import DevScanPlan, model_hash
from slagalpha.research.scan_trigger import ScanTriggerEvidence, revalidate_scan_trigger
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest
from slagalpha.strategy.plans import (
    EntryStopEvaluation,
    EntryStopRequest,
    TakeProfitEvaluation,
    build_entry_stop,
    build_take_profit,
)


class ScanTradePlanEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["scan-trade-plan-evidence/0.1.0"] = "scan-trade-plan-evidence/0.1.0"
    trigger_evidence: ScanTriggerEvidence
    registry_content_hash: Sha256
    rule_content_hash: Sha256
    universe_content_hash: Sha256
    status: Literal["SKIPPED_TRIGGER", "NOT_READY", "REJECTED_ENTRY_STOP",
                    "REJECTED_TAKE_PROFIT", "ACCEPTED_PLAN"]
    entry_stop: EntryStopEvaluation | None
    take_profit: TakeProfitEvaluation | None
    data_request: DevReplayDataRequest | None
    history_seed_verified: Literal[False] = False
    research_authorized: Literal[False] = False
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        decision = self.trigger_evidence.decision
        history = self.trigger_evidence.setup_evidence.features[0].history
        if self.universe_content_hash != history.universe_content_hash:
            raise ValueError("Trade Plan Universe must match its original scan history")
        if self.trigger_evidence.status == "NOT_READY":
            expected = "NOT_READY"
        elif decision is None or not decision.eligible_for_plan:
            expected = "SKIPPED_TRIGGER"
        elif self.entry_stop is None:
            raise ValueError("eligible trigger requires a P6 Entry/Stop result")
        elif not self.entry_stop.accepted:
            expected = "REJECTED_ENTRY_STOP"
        elif self.take_profit is None:
            raise ValueError("accepted Entry/Stop requires a P6 Take Profit result")
        else:
            expected = "ACCEPTED_PLAN" if self.take_profit.accepted else "REJECTED_TAKE_PROFIT"
        if self.status != expected:
            raise ValueError("Trade Plan status does not match its upstream results")
        if self.entry_stop is not None:
            if (decision is None or not decision.eligible_for_plan
                or self.entry_stop.request.logical_signal_id != decision.logical_signal_id
                or self.entry_stop.request.symbol != history.symbol
                or self.entry_stop.request.confirmation_close != history.confirmation_close):
                raise ValueError("Entry/Stop does not match the computed trigger")
        if self.take_profit is not None and (self.take_profit.entry_stop != self.entry_stop
                                            or not self.take_profit.entry_stop.accepted):
            raise ValueError("Take Profit must use the same accepted Entry/Stop")
        if self.status == "ACCEPTED_PLAN":
            if self.data_request is None or self.take_profit is None:
                raise ValueError("accepted Trade Plan requires a bounded data request")
            if (self.data_request.trade_plan_hash != model_hash(self.take_profit)
                or self.data_request.registry_content_hash != self.registry_content_hash
                or self.data_request.rule_content_hash != self.rule_content_hash
                or self.data_request.universe_content_hash != self.universe_content_hash):
                raise ValueError("bounded request must match the complete Trade Plan source")
        elif self.data_request is not None:
            raise ValueError("unaccepted Trade Plans cannot create data requests")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("Trade Plan evidence content hash mismatch")
        return self


def require_active_scan_rule(
    *, registry: ContractRegistry, symbol: str, at: datetime,
) -> ContractRegistryEntry:
    """Select the exact slot's reviewed rule; a daily audit does not prove intraday coverage."""
    matches = tuple(rule for rule in registry.entries if rule.symbol == symbol
                    and rule.effective_from <= at
                    and (rule.effective_to is None or at < rule.effective_to))
    if (len(matches) != 1 or matches[0].verification_status is not RegistryVerification.VERIFIED
        or matches[0].status != "TRADING" or matches[0].derived_first_candle_at > at
        or (matches[0].onboard_date is not None and matches[0].onboard_date > at)
        or (matches[0].inferred_delisted_at is not None and at >= matches[0].inferred_delisted_at)):
        raise CandleInputError("one VERIFIED active historical rule is required before P6")
    return matches[0]


def compute_scan_trade_plan(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    split: ResearchSplitManifest, universe: UniverseSnapshot, registry: ContractRegistry,
    trigger_evidence: ScanTriggerEvidence,
) -> ScanTradePlanEvidence:
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    universe = UniverseSnapshot.model_validate(universe.model_dump(mode="json"))
    registry = ContractRegistry.model_validate(registry.model_dump(mode="json"))
    trigger_evidence = ScanTriggerEvidence.model_validate(trigger_evidence.model_dump(mode="json"))
    first = trigger_evidence.setup_evidence.features[0]
    history = first.history
    at = history.confirmation_close
    if plan.blockers:
        raise CandleInputError("Trade Plan computation is blocked by DEV research inputs")
    if (split.split_hash != plan.split_hash
        or registry.registry_version != plan.contract_registry_version):
        raise CandleInputError("Trade Plan split or registry differs from the research plan")
    if (model_hash(universe) != history.universe_content_hash
        or not universe.effective_from <= at < universe.effective_to
        or history.symbol not in {member.symbol for member in universe.members}):
        raise CandleInputError("Trade Plan Universe differs from the exact scan slot")
    rule = require_active_scan_rule(registry=registry, symbol=history.symbol, at=at)
    revalidate_scan_trigger(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, evidence=trigger_evidence,
    )
    decision = trigger_evidence.decision
    entry_stop = None
    take_profit = None
    data_request = None
    status = "NOT_READY" if trigger_evidence.status == "NOT_READY" else "SKIPPED_TRIGGER"
    if decision is not None and decision.eligible_for_plan:
        candles, indicators = revalidate_scan_features(
            project_dir=project_dir, scan_plan=scan_plan, plan=plan, evidence=first,
        )
        hourly, _ = revalidate_scan_features(
            project_dir=project_dir, scan_plan=scan_plan, plan=plan,
            evidence=trigger_evidence.setup_evidence.features[1],
        )
        request = EntryStopRequest.from_trigger_decision(
            decision, confirmation_high=candles["high"].iloc[-1],
            confirmation_low=candles["low"].iloc[-1],
            atr_at_confirmation=float(indicators["atr14"].iloc[-1]), tick_size=rule.tick_size,
        )
        params = first.parameter.candidate.parameters
        entry_stop = build_entry_stop(
            request, entry_ttl_bars=params.entry_ttl_bars,
            stop_atr_multiplier=params.stop_atr_multiplier,
        )
        status = "REJECTED_ENTRY_STOP"
        if entry_stop.accepted:
            take_profit = build_take_profit(
                entry_stop, fifteen_minute_candles=candles, one_hour_candles=hourly,
                fifteen_minute_zones=first.zones,
                one_hour_zones=trigger_evidence.setup_evidence.features[1].zones,
            )
            status = "REJECTED_TAKE_PROFIT"
            if take_profit.accepted:
                data_request = build_dev_replay_data_request(
                    trade_plan=take_profit, plan=plan, parameter=first.parameter,
                    split=split, universe=universe, registry=registry,
                )
                status = "ACCEPTED_PLAN"
    payload = {
        "schema_version": "scan-trade-plan-evidence/0.1.0",
        "trigger_evidence": trigger_evidence.model_dump(mode="json"),
        "registry_content_hash": model_hash(registry), "rule_content_hash": model_hash(rule),
        "universe_content_hash": model_hash(universe), "status": status,
        "entry_stop": entry_stop.model_dump(mode="json") if entry_stop is not None else None,
        "take_profit": take_profit.model_dump(mode="json") if take_profit is not None else None,
        "data_request": data_request.model_dump(mode="json") if data_request is not None else None,
        "history_seed_verified": False, "research_authorized": False,
    }
    return ScanTradePlanEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def revalidate_scan_trade_plan(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    split: ResearchSplitManifest, universe: UniverseSnapshot, registry: ContractRegistry,
    evidence: ScanTradePlanEvidence,
) -> None:
    evidence = ScanTradePlanEvidence.model_validate(evidence.model_dump(mode="json"))
    observed = compute_scan_trade_plan(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, split=split,
        universe=universe, registry=registry, trigger_evidence=evidence.trigger_evidence,
    )
    if observed != evidence:
        raise CandleInputError("saved Trade Plan does not match recomputed source output")
