"""Default-centered, one-factor-at-a-time DEV research planning; no replay execution."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from slagalpha.backtest.costs import COST_RATES, CostRates, CostScenario
from slagalpha.backtest.replay import ALLOWED_MAX_HOLDING_BARS
from slagalpha.research.splits import (
    DatasetRole,
    ResearchInputAuditReport,
    ResearchReadiness,
    ResearchSplitManifest,
)
from slagalpha.strategy.pivots import ALLOWED_PIVOT_WINDOWS
from slagalpha.strategy.plans import ALLOWED_ENTRY_TTL_BARS, ALLOWED_STOP_ATR_MULTIPLIERS
from slagalpha.strategy.setup import ALLOWED_COMPRESSION_THRESHOLDS
from slagalpha.strategy.triggers import STRATEGY_VERSION


class SensitivityPlanError(ValueError):
    """Raised when research planning or its DEV input gate violates frozen rules."""


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class SensitivityParameters(BaseModel):
    """Exactly the five sensitivity axes permitted by the frozen V0.1 rules."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    pivot_window: tuple[int, int] = (2, 2)
    compression_threshold: Decimal = Decimal("0.75")
    stop_atr_multiplier: Decimal = Decimal("0.15")
    entry_ttl_bars: int = Field(default=4, strict=True)
    max_holding_bars: int = Field(default=32, strict=True)

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        if self.pivot_window not in ALLOWED_PIVOT_WINDOWS:
            raise ValueError("unsupported pivot_window")
        allowed_compression = {Decimal(str(value)) for value in ALLOWED_COMPRESSION_THRESHOLDS}
        if self.compression_threshold not in allowed_compression:
            raise ValueError("unsupported compression_threshold")
        if self.stop_atr_multiplier not in ALLOWED_STOP_ATR_MULTIPLIERS:
            raise ValueError("unsupported stop_atr_multiplier")
        if self.entry_ttl_bars not in ALLOWED_ENTRY_TTL_BARS:
            raise ValueError("unsupported entry_ttl_bars")
        if self.max_holding_bars not in ALLOWED_MAX_HOLDING_BARS:
            raise ValueError("unsupported max_holding_bars")
        return self

    @field_serializer("compression_threshold", "stop_atr_multiplier")
    def serialize_decimal(self, value: Decimal) -> str:
        return format(value, ".2f")


class SensitivityCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    changed_parameter: Literal[
        "BASELINE", "pivot_window", "compression_threshold", "stop_atr_multiplier",
        "entry_ttl_bars", "max_holding_bars",
    ]
    parameters: SensitivityParameters
    candidate_hash: str

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        baseline = SensitivityParameters().model_dump()
        changes = tuple(
            key for key, value in self.parameters.model_dump().items() if value != baseline[key]
        )
        expected = () if self.changed_parameter == "BASELINE" else (self.changed_parameter,)
        if changes != expected:
            raise ValueError("candidate must change exactly its one declared parameter")
        payload = self.model_dump(mode="json", exclude={"candidate_hash"})
        if self.candidate_hash != _hash(payload):
            raise ValueError("candidate content hash mismatch")
        return self


def default_candidates() -> tuple[SensitivityCandidate, ...]:
    """Enumerate the baseline and nine one-factor changes in a stable order."""

    baseline = SensitivityParameters()
    variants: list[tuple[str, SensitivityParameters]] = [("BASELINE", baseline)]
    axes: tuple[tuple[str, tuple[Any, ...]], ...] = (
        ("pivot_window", tuple(sorted(ALLOWED_PIVOT_WINDOWS))),
        ("compression_threshold", tuple(Decimal(str(v)) for v in sorted(
            ALLOWED_COMPRESSION_THRESHOLDS
        ))),
        ("stop_atr_multiplier", tuple(sorted(ALLOWED_STOP_ATR_MULTIPLIERS))),
        ("entry_ttl_bars", tuple(sorted(ALLOWED_ENTRY_TTL_BARS))),
        ("max_holding_bars", tuple(sorted(ALLOWED_MAX_HOLDING_BARS))),
    )
    for name, values in axes:
        for value in values:
            if value == getattr(baseline, name):
                continue
            parameters = SensitivityParameters.model_validate(
                {**baseline.model_dump(), name: value}
            )
            variants.append((name, parameters))
    candidates: list[SensitivityCandidate] = []
    for changed_parameter, parameters in variants:
        payload = {
            "changed_parameter": changed_parameter,
            "parameters": parameters.model_dump(mode="json"),
        }
        candidates.append(SensitivityCandidate.model_validate(
            {**payload, "candidate_hash": _hash(payload)}
        ))
    return tuple(candidates)


class SensitivityPlan(BaseModel):
    """Auditable DEV configuration plan; this is not a strategy run or parameter freeze."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["sensitivity-plan/0.1.0"] = "sensitivity-plan/0.1.0"
    strategy_version: str
    strategy_rules_sha256: str
    dataset_role: Literal[DatasetRole.DEV] = DatasetRole.DEV
    split_hash: str
    input_audit_hash: str
    contract_registry_version: str
    candidates: tuple[SensitivityCandidate, ...]
    cost_rates: dict[CostScenario, CostRates]
    planned_evaluation_count: int
    dev_input_readiness: ResearchReadiness
    blockers: tuple[str, ...]
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    plan_hash: str

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        for value in (self.strategy_rules_sha256, self.split_hash, self.input_audit_hash):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("plan evidence references must be lowercase SHA-256")
        if self.strategy_version != STRATEGY_VERSION:
            raise ValueError("unsupported sensitivity strategy version")
        if self.candidates != default_candidates():
            raise ValueError("plan must use the complete canonical default-centered candidate set")
        if self.cost_rates != dict(COST_RATES):
            raise ValueError("plan must retain all three frozen P7 cost scenarios")
        if self.planned_evaluation_count != len(self.candidates) * len(self.cost_rates):
            raise ValueError("planned_evaluation_count does not reconcile")
        if (self.dev_input_readiness is ResearchReadiness.BLOCKED) != bool(self.blockers):
            raise ValueError("DEV readiness and blockers disagree")
        if self.plan_hash != _hash(self.model_dump(mode="json", exclude={"plan_hash"})):
            raise ValueError("sensitivity plan content hash mismatch")
        return self


def build_default_sensitivity_plan(
    *,
    split: ResearchSplitManifest,
    audit: ResearchInputAuditReport,
    strategy_rules_sha256: str,
) -> SensitivityPlan:
    """Plan DEV only, binding the exact split and audit without relaxing any blocker."""

    split = ResearchSplitManifest.model_validate(split.model_dump(mode="json"))
    audit = ResearchInputAuditReport.model_validate(audit.model_dump(mode="json"))
    if (
        audit.split_hash != split.split_hash
        or audit.daily_snapshot_hash != split.daily_snapshot_hash
    ):
        raise SensitivityPlanError("input audit does not belong to this frozen split")
    dev = next(role for role in audit.roles if role.role is DatasetRole.DEV)
    payload = {
        "schema_version": "sensitivity-plan/0.1.0",
        "strategy_version": STRATEGY_VERSION,
        "strategy_rules_sha256": strategy_rules_sha256,
        "dataset_role": DatasetRole.DEV.value,
        "split_hash": split.split_hash,
        "input_audit_hash": audit.report_hash,
        "contract_registry_version": audit.contract_registry_version,
        "candidates": [candidate.model_dump(mode="json") for candidate in default_candidates()],
        "cost_rates": {scenario.value: rates.model_dump(mode="json")
                       for scenario, rates in COST_RATES.items()},
        "planned_evaluation_count": len(default_candidates()) * len(COST_RATES),
        "dev_input_readiness": dev.readiness.value,
        "blockers": list(dev.blockers),
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    return SensitivityPlan.model_validate({**payload, "plan_hash": _hash(payload)})


def require_dev_execution_inputs(
    plan: SensitivityPlan,
    audit: ResearchInputAuditReport,
) -> None:
    """Check the DEV prerequisite only, not authorization for replay/locked execution."""

    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    audit = ResearchInputAuditReport.model_validate(audit.model_dump(mode="json"))
    if (
        audit.report_hash != plan.input_audit_hash
        or audit.split_hash != plan.split_hash
        or audit.contract_registry_version != plan.contract_registry_version
    ):
        raise SensitivityPlanError("DEV input audit does not match the research plan")
    dev = next(role for role in audit.roles if role.role is DatasetRole.DEV)
    if dev.readiness is not ResearchReadiness.READY or plan.blockers:
        raise SensitivityPlanError("DEV research blocked: " + "; ".join(dev.blockers))


def write_sensitivity_plan(plan: SensitivityPlan, data_dir: Path) -> Path:
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    destination = data_dir / "manifests" / "sensitivity_plan" / f"{plan.plan_hash}.json"
    content = (json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise SensitivityPlanError(f"existing sensitivity plan changed: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
