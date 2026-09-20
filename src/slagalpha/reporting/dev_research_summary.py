"""Compact 10-candidate by 3-cost DEV research comparison schema."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.backtest.costs import CostScenario
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.sensitivity import SensitivityCandidate, SensitivityPlan

SCENARIO_ORDER = (CostScenario.ZERO, CostScenario.BASELINE, CostScenario.STRESS)


class ScenarioResearchSummary(BaseModel):
    """Metrics needed to compare one candidate under one frozen cost scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    scenario: CostScenario
    request_count: int = Field(ge=0, strict=True)
    executed_count: int = Field(ge=0, strict=True)
    skipped_active_count: int = Field(ge=0, strict=True)
    closed_trade_count: int = Field(ge=0, strict=True)
    net_eligible_count: int = Field(ge=0, strict=True)
    winner_count: int = Field(ge=0, strict=True)
    win_rate: Decimal | None
    total_net_r: Decimal | None
    expectancy_net_r: Decimal | None
    max_drawdown_r: Decimal | None
    total_fee: Decimal = Field(ge=0)
    total_slippage_cost: Decimal = Field(ge=0)
    total_funding_cash_flow: Decimal
    data_warning_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_metrics(self) -> Self:
        if self.executed_count + self.skipped_active_count != self.request_count:
            raise ValueError("executed and skipped counts must cover every request")
        if not (
            self.winner_count <= self.net_eligible_count <= self.closed_trade_count
            <= self.executed_count
        ):
            raise ValueError("research result counts do not reconcile")
        net_metrics = (
            self.win_rate,
            self.total_net_r,
            self.expectancy_net_r,
            self.max_drawdown_r,
        )
        if self.net_eligible_count == 0 and any(value is not None for value in net_metrics):
            raise ValueError("Net metrics require at least one eligible trade")
        if self.net_eligible_count > 0 and any(value is None for value in net_metrics):
            raise ValueError("eligible trades require complete Net metrics")
        if self.win_rate is not None and not Decimal(0) <= self.win_rate <= Decimal(1):
            raise ValueError("win rate must be in [0, 1]")
        return self


class CandidateResearchSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: SensitivityCandidate
    scenarios: tuple[ScenarioResearchSummary, ...]

    @model_validator(mode="after")
    def validate_scenarios(self) -> Self:
        if tuple(item.scenario for item in self.scenarios) != SCENARIO_ORDER:
            raise ValueError("candidate must contain ZERO, BASELINE, STRESS in order")
        if len({item.request_count for item in self.scenarios}) != 1:
            raise ValueError("cost scenarios must evaluate the same request count")
        return self


class P9DevResearchSummary(BaseModel):
    """Frozen comparison matrix; it cannot represent VALIDATION or LOCKED_TEST."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    schema_version: Literal["p9-dev-research-summary/0.1.0"] = (
        "p9-dev-research-summary/0.1.0"
    )
    dataset_role: Literal["DEV"] = "DEV"
    source_scan_report_hash: Sha256
    request_set_hash: Sha256
    sensitivity_plan_hash: Sha256
    candidate_count: Literal[10] = 10
    scenario_count: Literal[3] = 3
    evaluation_count: Literal[30] = 30
    candidates: tuple[CandidateResearchSummary, ...]
    locked_test_consumed: Literal[False] = False
    summary_hash: Sha256

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if len(self.candidates) != self.candidate_count:
            raise ValueError("research summary requires exactly ten candidates")
        candidate_hashes = tuple(item.candidate.candidate_hash for item in self.candidates)
        if len(set(candidate_hashes)) != self.candidate_count:
            raise ValueError("research summary candidates must be unique")
        if sum(len(item.scenarios) for item in self.candidates) != self.evaluation_count:
            raise ValueError("research summary evaluation count does not reconcile")
        payload = self.model_dump(mode="json", exclude={"summary_hash"})
        if self.summary_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("DEV research summary content hash mismatch")
        return self


def build_p9_dev_research_summary(
    *,
    plan: SensitivityPlan,
    source_scan_report_hash: str,
    request_set_hash: str,
    candidates: tuple[CandidateResearchSummary, ...],
) -> P9DevResearchSummary:
    """Bind already-aggregated DEV metrics to the exact frozen comparison plan."""

    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    candidates = tuple(
        CandidateResearchSummary.model_validate(item.model_dump(mode="json"))
        for item in candidates
    )
    if tuple(item.candidate for item in candidates) != plan.candidates:
        raise ValueError("research summaries do not match the frozen candidate plan")
    payload = {
        "schema_version": "p9-dev-research-summary/0.1.0",
        "dataset_role": "DEV",
        "source_scan_report_hash": source_scan_report_hash,
        "request_set_hash": request_set_hash,
        "sensitivity_plan_hash": plan.plan_hash,
        "candidate_count": 10,
        "scenario_count": 3,
        "evaluation_count": 30,
        "candidates": [item.model_dump(mode="json") for item in candidates],
        "locked_test_consumed": False,
    }
    return P9DevResearchSummary.model_validate(
        {**payload, "summary_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}
    )
