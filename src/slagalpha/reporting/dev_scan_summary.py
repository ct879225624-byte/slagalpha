"""Human-readable diagnostics for a completed DEV source scan report."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.replay_inputs import Sha256


class CountedLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    count: int = Field(ge=0, strict=True)


class DevScanHumanSummary(BaseModel):
    """Compact structured summary behind the plain-language rendering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["dev-source-scan-summary/0.1.0"] = (
        "dev-source-scan-summary/0.1.0"
    )
    source_report_hash: Sha256
    status: Literal["COMPLETE", "PARTIAL"]
    completed_symbol_count: int = Field(ge=0, strict=True)
    universe_symbol_count: int = Field(gt=0, strict=True)
    completed_symbols: tuple[str, ...]
    scanned_slot_count: int = Field(ge=0, strict=True)
    setup_evaluated_count: int = Field(ge=0, strict=True)
    setup_eligible_count: int = Field(ge=0, strict=True)
    trigger_evaluated_count: int = Field(ge=0, strict=True)
    trigger_confirmed_count: int = Field(ge=0, strict=True)
    accepted_count: int = Field(ge=0, strict=True)
    accepted_symbols: tuple[str, ...]
    accepted_by_month: tuple[CountedLabel, ...]
    accepted_by_utc_hour: tuple[CountedLabel, ...]
    top_symbols: tuple[CountedLabel, ...]
    anomaly_groups: tuple[CountedLabel, ...]
    diagnostics: tuple[str, ...]
    summary_hash: Sha256

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.completed_symbol_count != len(self.completed_symbols):
            raise ValueError("completed symbol count does not reconcile")
        if self.completed_symbols != tuple(sorted(set(self.completed_symbols))):
            raise ValueError("completed symbols must be unique and canonical")
        if self.accepted_symbols != tuple(sorted(set(self.accepted_symbols))):
            raise ValueError("accepted symbols must be unique and canonical")
        payload = self.model_dump(mode="json", exclude={"summary_hash"})
        if self.summary_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("scan summary content hash mismatch")
        return self


def _counts(values: Counter[str]) -> tuple[CountedLabel, ...]:
    return tuple(
        CountedLabel(label=label, count=count)
        for label, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))
    )


def build_dev_scan_summary(
    report: DevSourceScanReport, *, top_symbol_limit: int = 10
) -> DevScanHumanSummary:
    """Summarize only a final report; this never reads checkpoints or scan inputs."""

    report = DevSourceScanReport.model_validate(report.model_dump(mode="json"))
    if top_symbol_limit < 1:
        raise ValueError("top_symbol_limit must be positive")
    symbols: Counter[str] = Counter()
    months: Counter[str] = Counter()
    hours: Counter[str] = Counter()
    for accepted in report.accepted_requests:
        request = accepted.request
        symbols[request.request.armed.symbol] += 1
        months[request.start.strftime("%Y-%m")] += 1
        hours[f"{request.start.hour:02d}:00 UTC"] += 1
    anomaly_groups = Counter(
        anomaly.partition(":")[0].strip() or "UNCLASSIFIED" for anomaly in report.anomalies
    )

    diagnostics: list[str] = []
    if report.status == "PARTIAL":
        diagnostics.append("扫描报告不完整，不能据此启动行情获取或研究。")
    if len(report.scanned_symbols) < report.universe_symbol_count:
        diagnostics.append(
            f"成功完成 {len(report.scanned_symbols)}/{report.universe_symbol_count} 个 symbol。"
        )
    if report.funnel.source_error_slot_count:
        diagnostics.append(
            f"有 {report.funnel.source_error_slot_count} 个 slot 发生来源错误。"
        )
    if report.validated_source_partition_count != report.source_partition_count:
        diagnostics.append(
            "来源分区未全部通过校验："
            f"{report.validated_source_partition_count}/{report.source_partition_count}。"
        )
    if not report.accepted_requests:
        diagnostics.append("结果为零：没有 accepted P6 request，需先检查漏斗与数据覆盖。")
    elif len(symbols) > 1 and report.funnel.accepted_count >= 10:
        top_symbol, top_count = symbols.most_common(1)[0]
        if top_count * 2 >= report.funnel.accepted_count:
            diagnostics.append(
                f"accepted 结果集中在 {top_symbol}：{top_count}/"
                f"{report.funnel.accepted_count}。"
            )
    if len(months) > 1 and report.funnel.accepted_count >= 10:
        top_month, top_count = months.most_common(1)[0]
        if top_count * 2 >= report.funnel.accepted_count:
            diagnostics.append(
                f"accepted 时间集中在 {top_month}：{top_count}/"
                f"{report.funnel.accepted_count}。"
            )
    if report.anomalies:
        top_group, top_count = anomaly_groups.most_common(1)[0]
        diagnostics.append(
            f"共 {len(report.anomalies)} 条异常，最多的是 {top_group}（{top_count} 条）。"
        )
    if not diagnostics:
        diagnostics.append("未发现零结果、异常集中或明显来源完整性问题。")

    payload = {
        "schema_version": "dev-source-scan-summary/0.1.0",
        "source_report_hash": report.report_hash,
        "status": report.status,
        "completed_symbol_count": len(report.scanned_symbols),
        "universe_symbol_count": report.universe_symbol_count,
        "completed_symbols": list(report.scanned_symbols),
        "scanned_slot_count": report.funnel.scan_slot_count,
        "setup_evaluated_count": report.funnel.setup_evaluated_count,
        "setup_eligible_count": report.funnel.setup_eligible_count,
        "trigger_evaluated_count": report.funnel.trigger_evaluated_count,
        "trigger_confirmed_count": report.funnel.trigger_confirmed_count,
        "accepted_count": report.funnel.accepted_count,
        "accepted_symbols": list(report.accepted_symbols),
        "accepted_by_month": [item.model_dump() for item in _counts(months)],
        "accepted_by_utc_hour": [item.model_dump() for item in _counts(hours)],
        "top_symbols": [item.model_dump() for item in _counts(symbols)[:top_symbol_limit]],
        "anomaly_groups": [item.model_dump() for item in _counts(anomaly_groups)],
        "diagnostics": diagnostics,
    }
    return DevScanHumanSummary.model_validate(
        {
            **payload,
            "summary_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        }
    )


def _format_counts(values: tuple[CountedLabel, ...]) -> str:
    return "、".join(f"{item.label}={item.count}" for item in values) or "无"


def render_dev_scan_summary(summary: DevScanHumanSummary) -> str:
    """Render a short Chinese summary suitable for direct human review."""

    summary = DevScanHumanSummary.model_validate(summary.model_dump(mode="json"))
    accepted = "、".join(summary.accepted_symbols) or "无"
    return "\n".join(
        (
            f"扫描状态：{summary.status}；完成 symbols "
            f"{summary.completed_symbol_count}/{summary.universe_symbol_count}，"
            f"共扫描 {summary.scanned_slot_count} 个 slots。",
            f"漏斗：setup 已评估 {summary.setup_evaluated_count}、符合 "
            f"{summary.setup_eligible_count}；trigger 已评估 "
            f"{summary.trigger_evaluated_count}、确认 {summary.trigger_confirmed_count}；"
            f"accepted {summary.accepted_count}。",
            f"Accepted symbols：{accepted}。",
            f"按月分布：{_format_counts(summary.accepted_by_month)}。",
            f"按确认时刻分布：{_format_counts(summary.accepted_by_utc_hour)}。",
            f"Top symbols：{_format_counts(summary.top_symbols)}。",
            "诊断：" + " ".join(summary.diagnostics),
        )
    ) + "\n"
