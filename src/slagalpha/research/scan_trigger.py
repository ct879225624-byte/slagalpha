"""Recompute P5 from the exact P4 context and its verified point-in-time features."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.scan_features import revalidate_scan_features
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.scan_setup import ScanSetupEvidence, revalidate_scan_setup
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.strategy.setup import PullbackState
from slagalpha.strategy.triggers import (
    TriggerDecision,
    TriggerNotReadyError,
    TriggerSetupContext,
    evaluate_triggers,
)


class ScanTriggerEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["scan-trigger-evidence/0.1.0"] = "scan-trigger-evidence/0.1.0"
    setup_evidence: ScanSetupEvidence
    status: Literal["SKIPPED_SETUP", "NOT_READY", "EVALUATED"]
    trigger_context: TriggerSetupContext | None
    decision: TriggerDecision | None
    not_ready_detail: str | None
    history_seed_verified: Literal[False] = False
    research_authorized: Literal[False] = False
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        setup = self.setup_evidence.setup
        if self.setup_evidence.status == "NOT_READY":
            if (self.status != "NOT_READY" or self.trigger_context is not None
                or self.not_ready_detail != self.setup_evidence.not_ready_detail):
                raise ValueError("P4 NOT_READY must propagate without a fabricated trigger context")
        elif setup is not None and not setup.eligible_for_trigger:
            if self.status != "SKIPPED_SETUP" or self.trigger_context is not None:
                raise ValueError("ineligible P4 must skip trigger evaluation")
        else:
            if self.trigger_context is None or setup is None or setup.one_hour is None:
                raise ValueError("eligible P4 requires a bound trigger context")
            if (self.trigger_context.symbol != setup.symbol
                or self.trigger_context.direction != setup.four_hour.direction
                or self.trigger_context.episode_id != setup.one_hour.episode_id
                or self.trigger_context.episode_start != setup.one_hour.episode_start
                or self.trigger_context.pullback_state != setup.one_hour.state
                or not self.trigger_context.eligible_for_trigger):
                raise ValueError("trigger context does not match P4 episode")
            if self.status not in ("EVALUATED", "NOT_READY"):
                raise ValueError("eligible P4 cannot be skipped")
        if self.status == "EVALUATED":
            history = self.setup_evidence.features[0].history
            if (self.decision is None or self.not_ready_detail is not None
                or self.decision.symbol != history.symbol
                or self.decision.confirmation_close_time != history.confirmation_close):
                raise ValueError("evaluated trigger must match the exact scan slot")
        elif self.decision is not None:
            raise ValueError("uncomputed triggers cannot contain a decision")
        if (self.status == "NOT_READY") != bool(self.not_ready_detail):
            raise ValueError("trigger NOT_READY must have an explicit history detail")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("trigger evidence content hash mismatch")
        return self


def compute_scan_trigger(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    setup_evidence: ScanSetupEvidence,
) -> ScanTriggerEvidence:
    setup_evidence = ScanSetupEvidence.model_validate(setup_evidence.model_dump(mode="json"))
    revalidate_scan_setup(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, evidence=setup_evidence,
    )
    setup = setup_evidence.setup
    detail = setup_evidence.not_ready_detail
    status = "NOT_READY" if detail is not None else "SKIPPED_SETUP"
    context = None
    decision = None
    if setup is not None and setup.eligible_for_trigger:
        fifteen, fifteen_indicators = revalidate_scan_features(
            project_dir=project_dir, scan_plan=scan_plan, plan=plan,
            evidence=setup_evidence.features[0],
        )
        _, hourly_indicators = revalidate_scan_features(
            project_dir=project_dir, scan_plan=scan_plan, plan=plan,
            evidence=setup_evidence.features[1],
        )
        hourly = setup.one_hour
        if (hourly is None or hourly.episode_id is None or hourly.episode_start is None
            or hourly.state not in (PullbackState.SHALLOW, PullbackState.STANDARD)):
            raise CandleInputError("eligible P4 lacks a valid pullback episode")
        columns = (("sma30", "sma60") if hourly.state is PullbackState.SHALLOW
                   else ("sma60", "sma90"))
        bounds = sorted(float(hourly_indicators[column].iloc[-1]) for column in columns)
        context = TriggerSetupContext(
            symbol=setup.symbol, direction=setup.four_hour.direction, pullback_state=hourly.state,
            eligible_for_trigger=True, episode_id=hourly.episode_id,
            episode_start=hourly.episode_start,
            region_lower=bounds[0], region_upper=bounds[1],
        )
        try:
            decision = evaluate_triggers(
                fifteen, fifteen_indicators, setup=context,
                pivots=setup_evidence.features[0].pivots, zones=setup_evidence.features[0].zones,
            )
            status = "EVALUATED"
        except TriggerNotReadyError as error:
            detail = str(error)
            status = "NOT_READY"
    payload = {
        "schema_version": "scan-trigger-evidence/0.1.0",
        "setup_evidence": setup_evidence.model_dump(mode="json"), "status": status,
        "trigger_context": context.model_dump(mode="json") if context is not None else None,
        "decision": decision.model_dump(mode="json") if decision is not None else None,
        "not_ready_detail": detail, "history_seed_verified": False, "research_authorized": False,
    }
    return ScanTriggerEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def revalidate_scan_trigger(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    evidence: ScanTriggerEvidence,
) -> None:
    evidence = ScanTriggerEvidence.model_validate(evidence.model_dump(mode="json"))
    observed = compute_scan_trigger(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan,
        setup_evidence=evidence.setup_evidence,
    )
    if observed != evidence:
        raise CandleInputError("saved trigger evidence does not match recomputed source output")
