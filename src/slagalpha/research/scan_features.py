"""Recompute point-in-time P3 features from verified prefixes without research authorization."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, model_validator

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_history import ScanCandleHistory, reload_scan_candle_history
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.parameters import DevParameterVersion, require_parameter_plan_binding
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.strategy.indicators import compute_indicator_frame
from slagalpha.strategy.pivots import (
    PivotEvent,
    PivotZone,
    detect_confirmed_pivots,
    merge_pivot_zones,
)


def indicator_frame_hash(frame: pd.DataFrame) -> str:
    """Hash exact float64 values and explicit warm-up nulls, never JSON NaN/Infinity."""
    rows = []
    for row in frame.itertuples(index=False, name=None):
        values: list[str | None] = []
        for cell in row:
            number = float(cell)
            if math.isinf(number):
                raise CandleInputError("computed indicator contains infinity")
            values.append(None if math.isnan(number) else number.hex())
        rows.append(values)
    payload = {"columns": list(frame.columns), "rows": rows}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class ScanFeatureEvidence(BaseModel):
    """Recomputable output for a declared seed; model parsing alone proves no computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["scan-feature-evidence/0.1.0"] = "scan-feature-evidence/0.1.0"
    history: ScanCandleHistory
    parameter: DevParameterVersion
    indicator_version: Literal["indicators/0.1.0"] = "indicators/0.1.0"
    pivot_version: Literal["confirmed-pivot/0.1.0"] = "confirmed-pivot/0.1.0"
    zone_version: Literal["pivot-zone/0.1.0"] = "pivot-zone/0.1.0"
    indicator_content_hash: Sha256
    pivots: tuple[PivotEvent, ...]
    zones: tuple[PivotZone, ...]
    history_seed_verified: Literal[False] = False
    strategy_executed: Literal[False] = False
    research_authorized: Literal[False] = False
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        window = self.parameter.candidate.parameters.pivot_window
        if any(pivot.interval != self.history.interval
               or (pivot.left, pivot.right) != window
               or pivot.confirmed_at > self.history.last_close_exclusive
               or pivot.pivot_time < self.history.history_start
               or pivot.confirmed_position >= self.history.row_count for pivot in self.pivots):
            raise ValueError("Pivot evidence lies outside the exact prefix or candidate")
        if self.zones != merge_pivot_zones(self.pivots):
            raise ValueError("Zone evidence does not match canonical Pivot merging")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("feature evidence content hash mismatch")
        return self


def compute_scan_features(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    parameter: DevParameterVersion, history: ScanCandleHistory,
) -> tuple[pd.DataFrame, pd.DataFrame, ScanFeatureEvidence]:
    """Revalidate raw input first and use the candidate's exact Pivot window; no P4-P7 run."""
    scan_plan = DevScanPlan.model_validate(scan_plan.model_dump(mode="json"))
    plan = SensitivityPlan.model_validate(plan.model_dump(mode="json"))
    parameter = DevParameterVersion.model_validate(parameter.model_dump(mode="json"))
    require_parameter_plan_binding(parameter, plan)
    if plan.blockers:
        raise CandleInputError("feature computation is blocked by the DEV input plan")
    if (scan_plan.sensitivity_plan_hash != plan.plan_hash
        or scan_plan.parameter_content_hash != parameter.content_hash):
        raise CandleInputError("feature inputs do not belong to the same scan candidate")
    candles = reload_scan_candle_history(
        project_dir=project_dir, scan_plan=scan_plan, history=history,
    )
    indicators = compute_indicator_frame(candles)
    left, right = parameter.candidate.parameters.pivot_window
    pivots = detect_confirmed_pivots(candles, indicators, history.interval, left=left, right=right)
    zones = merge_pivot_zones(pivots)
    payload = {
        "schema_version": "scan-feature-evidence/0.1.0", "history": history.model_dump(mode="json"),
        "parameter": parameter.model_dump(mode="json"), "indicator_version": "indicators/0.1.0",
        "pivot_version": "confirmed-pivot/0.1.0", "zone_version": "pivot-zone/0.1.0",
        "indicator_content_hash": indicator_frame_hash(indicators),
        "pivots": [pivot.model_dump(mode="json") for pivot in pivots],
        "zones": [zone.model_dump(mode="json") for zone in zones],
        "history_seed_verified": False, "strategy_executed": False, "research_authorized": False,
    }
    evidence = ScanFeatureEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })
    return candles, indicators, evidence


def revalidate_scan_features(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    evidence: ScanFeatureEvidence,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    evidence = ScanFeatureEvidence.model_validate(evidence.model_dump(mode="json"))
    candles, indicators, observed = compute_scan_features(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan,
        parameter=evidence.parameter, history=evidence.history,
    )
    if observed != evidence:
        raise CandleInputError("saved feature evidence does not match recomputed source output")
    return candles, indicators
