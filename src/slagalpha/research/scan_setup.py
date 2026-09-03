"""P4 recomputation from one bound four-timeframe feature bundle, not a research execution gate."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.replay_inputs import Sha256
from slagalpha.research.scan_features import ScanFeatureEvidence, revalidate_scan_features
from slagalpha.research.scan_plan import DevScanPlan
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.strategy.setup import (
    SetupContextEvaluation,
    SetupNotReadyError,
    evaluate_setup_context,
)

FEATURE_INTERVALS = ("15m", "1h", "4h", "1d")


def require_same_scan_feature_bundle(features: tuple[ScanFeatureEvidence, ...]) -> None:
    if tuple(item.history.interval for item in features) != FEATURE_INTERVALS:
        raise CandleInputError("Setup requires exactly 15m, 1h, 4h, 1d features in canonical order")
    identities = {
        (item.history.scan_plan_hash, item.history.universe_content_hash,
         item.history.symbol, item.history.confirmation_close, item.parameter.content_hash)
        for item in features
    }
    if len(identities) != 1:
        raise CandleInputError("all Setup features must belong to the same scan slot and candidate")


class ScanSetupEvidence(BaseModel):
    """A P4 result over declared seeds, including a distinct insufficient-history state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["scan-setup-evidence/0.1.0"] = "scan-setup-evidence/0.1.0"
    features: tuple[ScanFeatureEvidence, ...] = Field(min_length=4, max_length=4)
    status: Literal["EVALUATED", "NOT_READY"]
    setup: SetupContextEvaluation | None
    not_ready_detail: str | None
    history_seed_verified: Literal[False] = False
    research_authorized: Literal[False] = False
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        require_same_scan_feature_bundle(self.features)
        if self.status == "EVALUATED":
            if self.setup is None or self.not_ready_detail is not None:
                raise ValueError("evaluated Setup requires a P4 result without warm-up error")
            if (self.setup.symbol != self.features[0].history.symbol
                or self.setup.four_hour.compression_threshold != float(
                    self.features[0].parameter.candidate.parameters.compression_threshold
                )):
                raise ValueError("Setup result does not match its feature symbol and candidate")
        elif (self.setup is not None or not self.not_ready_detail
              or not self.not_ready_detail.strip()):
            raise ValueError("NOT_READY requires a history detail and no fabricated Setup result")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        if self.content_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("Setup evidence content hash mismatch")
        return self


def compute_scan_setup(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    features: tuple[ScanFeatureEvidence, ...],
) -> ScanSetupEvidence:
    features = tuple(ScanFeatureEvidence.model_validate(item.model_dump(mode="json"))
                     for item in features)
    require_same_scan_feature_bundle(features)
    frames = {
        item.history.interval: revalidate_scan_features(
            project_dir=project_dir, scan_plan=scan_plan, plan=plan, evidence=item,
        ) for item in features
    }
    four_candles, four_indicators = frames["4h"]
    daily_candles, daily_indicators = frames["1d"]
    hourly_candles, hourly_indicators = frames["1h"]
    detail = None
    setup = None
    try:
        setup = evaluate_setup_context(
            symbol=features[0].history.symbol,
            four_hour_candles=four_candles, four_hour_indicators=four_indicators,
            daily_candles=daily_candles, daily_indicators=daily_indicators,
            one_hour_candles=hourly_candles, one_hour_indicators=hourly_indicators,
            compression_threshold=float(features[0].parameter.candidate.parameters.compression_threshold),
        )
    except SetupNotReadyError as error:
        detail = str(error)
    payload = {
        "schema_version": "scan-setup-evidence/0.1.0",
        "features": [item.model_dump(mode="json") for item in features],
        "status": "NOT_READY" if detail is not None else "EVALUATED",
        "setup": setup.model_dump(mode="json") if setup is not None else None,
        "not_ready_detail": detail, "history_seed_verified": False, "research_authorized": False,
    }
    return ScanSetupEvidence.model_validate({
        **payload, "content_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    })


def revalidate_scan_setup(
    *, project_dir: Path, scan_plan: DevScanPlan, plan: SensitivityPlan,
    evidence: ScanSetupEvidence,
) -> None:
    evidence = ScanSetupEvidence.model_validate(evidence.model_dump(mode="json"))
    observed = compute_scan_setup(
        project_dir=project_dir, scan_plan=scan_plan, plan=plan, features=evidence.features,
    )
    if observed != evidence:
        raise CandleInputError("saved Setup evidence does not match recomputed source output")
