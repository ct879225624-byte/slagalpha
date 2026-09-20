"""Request-scoped market-data planning with no network or download capability."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.replay_inputs import Sha256


class MarketDataRequirementError(ValueError):
    """A scan report cannot safely define a complete acquisition scope."""


class RequirementWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime
    end_exclusive: datetime
    request_hashes: tuple[Sha256, ...] = Field(min_length=1)
    one_minute_record_count: int = Field(gt=0, strict=True)
    funding_record_filter: Literal["start <= fundingTime < end_exclusive"] = (
        "start <= fundingTime < end_exclusive"
    )
    funding_record_count_estimate: None = None

    @field_validator("start", "end_exclusive")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("requirement window timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.end_exclusive <= self.start:
            raise ValueError("requirement window must be non-empty")
        if self.request_hashes != tuple(sorted(set(self.request_hashes))):
            raise ValueError("window request hashes must be unique and canonical")
        minutes = int((self.end_exclusive - self.start).total_seconds() // 60)
        if self.one_minute_record_count != minutes:
            raise ValueError("1m estimate must equal the exact window minutes")
        return self


class SymbolMarketDataRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    windows: tuple[RequirementWindow, ...] = Field(min_length=1)
    one_minute_record_count: int = Field(gt=0, strict=True)
    funding_record_count_estimate: None = None

    @model_validator(mode="after")
    def validate_symbol(self) -> Self:
        if not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError("symbol must be non-empty and unpadded")
        if tuple((item.start, item.end_exclusive) for item in self.windows) != tuple(
            sorted((item.start, item.end_exclusive) for item in self.windows)
        ):
            raise ValueError("symbol windows must be ordered")
        if any(
            current.start <= previous.end_exclusive
            for previous, current in zip(self.windows, self.windows[1:], strict=False)
        ):
            raise ValueError("symbol windows must already be merged")
        if self.one_minute_record_count != sum(
            item.one_minute_record_count for item in self.windows
        ):
            raise ValueError("symbol 1m count does not reconcile")
        return self


class MarketDataRequirementPlan(BaseModel):
    """Exact Candle scope and Funding selection scope, never download authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-market-data-requirements/0.1.0"] = (
        "dev-market-data-requirements/0.1.0"
    )
    source_report_hash: Sha256
    request_set_hash: Sha256
    request_count: int = Field(ge=0, strict=True)
    symbols: tuple[str, ...]
    requirements: tuple[SymbolMarketDataRequirement, ...]
    one_minute_rows_before_overlap_dedup: int = Field(ge=0, strict=True)
    one_minute_rows_after_overlap_dedup: int = Field(ge=0, strict=True)
    funding_windows_after_overlap_dedup: int = Field(ge=0, strict=True)
    funding_record_count_estimate: None = None
    funding_estimate_basis: Literal["EXTERNAL_VERSIONED_SCHEDULE_REQUIRED"] = (
        "EXTERNAL_VERSIONED_SCHEDULE_REQUIRED"
    )
    download_authorized: Literal[False] = False
    network_accessed: Literal[False] = False
    plan_hash: Sha256

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.symbols != tuple(item.symbol for item in self.requirements):
            raise ValueError("symbols do not match ordered requirements")
        if self.symbols != tuple(sorted(set(self.symbols))):
            raise ValueError("symbols must be unique and canonical")
        if self.one_minute_rows_after_overlap_dedup != sum(
            item.one_minute_record_count for item in self.requirements
        ):
            raise ValueError("deduplicated 1m total does not reconcile")
        if self.funding_windows_after_overlap_dedup != sum(
            len(item.windows) for item in self.requirements
        ):
            raise ValueError("Funding window total does not reconcile")
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("market-data requirement plan hash mismatch")
        return self


def _merged_windows(
    values: list[tuple[datetime, datetime, str]],
) -> tuple[RequirementWindow, ...]:
    merged: list[tuple[datetime, datetime, set[str]]] = []
    for start, end, request_hash in sorted(values):
        if merged and start <= merged[-1][1]:
            prior_start, prior_end, hashes = merged[-1]
            hashes.add(request_hash)
            merged[-1] = (prior_start, max(prior_end, end), hashes)
        else:
            merged.append((start, end, {request_hash}))
    return tuple(
        RequirementWindow(
            start=start,
            end_exclusive=end,
            request_hashes=tuple(sorted(hashes)),
            one_minute_record_count=int((end - start).total_seconds() // 60),
        )
        for start, end, hashes in merged
    )


def build_market_data_requirement_plan(
    report: DevSourceScanReport,
) -> MarketDataRequirementPlan:
    """Derive exact request windows only from an authenticated complete scan report."""

    report = DevSourceScanReport.model_validate(report.model_dump(mode="json"))
    if report.status != "COMPLETE":
        raise MarketDataRequirementError(
            "a partial scan report cannot authorize a complete market-data plan"
        )
    grouped: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
    for accepted in report.accepted_requests:
        request = accepted.request
        grouped[request.request.armed.symbol].append(
            (request.start, request.end_exclusive, request.request_hash)
        )
    requirements = tuple(
        SymbolMarketDataRequirement(
            symbol=symbol,
            windows=(windows := _merged_windows(grouped[symbol])),
            one_minute_record_count=sum(item.one_minute_record_count for item in windows),
        )
        for symbol in sorted(grouped)
    )
    payload = {
        "schema_version": "dev-market-data-requirements/0.1.0",
        "source_report_hash": report.report_hash,
        "request_set_hash": report.request_set_hash,
        "request_count": len(report.accepted_requests),
        "symbols": [item.symbol for item in requirements],
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "one_minute_rows_before_overlap_dedup": sum(
            item.request.expected_candle_count for item in report.accepted_requests
        ),
        "one_minute_rows_after_overlap_dedup": sum(
            item.one_minute_record_count for item in requirements
        ),
        "funding_windows_after_overlap_dedup": sum(
            len(item.windows) for item in requirements
        ),
        "funding_record_count_estimate": None,
        "funding_estimate_basis": "EXTERNAL_VERSIONED_SCHEDULE_REQUIRED",
        "download_authorized": False,
        "network_accessed": False,
    }
    return MarketDataRequirementPlan.model_validate(
        {**payload, "plan_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}
    )


def render_market_data_requirement_plan(plan: MarketDataRequirementPlan) -> str:
    """Render the exact request scope without implying that any download occurred."""

    plan = MarketDataRequirementPlan.model_validate(plan.model_dump(mode="json"))
    lines = [
        f"行情需求：{plan.request_count} 个 accepted requests，{len(plan.symbols)} 个 symbols。",
        "Funding 数量不硬编码；只获取下列窗口内由版本化真实日程给出的结算记录。",
    ]
    for item in plan.requirements:
        lines.append(f"{item.symbol}：1m 预计 {item.one_minute_record_count} 行")
        lines.extend(
            f"  [{window.start.isoformat()}, {window.end_exclusive.isoformat()})；"
            f"1m={window.one_minute_record_count}，Funding=待真实日程确定"
            for window in item.windows
        )
    lines.append(
        "合计：1m 去重前 "
        f"{plan.one_minute_rows_before_overlap_dedup} 行，去重后 "
        f"{plan.one_minute_rows_after_overlap_dedup} 行；Funding 查询窗口 "
        f"{plan.funding_windows_after_overlap_dedup} 个。"
    )
    return "\n".join(lines) + "\n"
