"""Create offline summary and market-data requirements from one final scan report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from slagalpha.reporting.dev_scan_summary import (
    build_dev_scan_summary,
    render_dev_scan_summary,
)
from slagalpha.reporting.run_manifest import _publish_immutable
from slagalpha.research.dev_source_scan import DevSourceScanReport
from slagalpha.research.market_data_requirements import (
    build_market_data_requirement_plan,
    render_market_data_requirement_plan,
)


def _write_new_or_equal(path: Path, content: bytes) -> None:
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError(f"existing post-scan artifact changed: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(path, content)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize a final P9 DEV scan and plan request-scoped market data offline."
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/post_scan"))
    args = parser.parse_args()

    report = DevSourceScanReport.model_validate_json(args.report.read_bytes())
    summary = build_dev_scan_summary(report)
    stem = report.report_hash
    summary_text = render_dev_scan_summary(summary)
    _write_new_or_equal(args.output_dir / f"{stem}.summary.txt", summary_text.encode("utf-8"))
    _write_new_or_equal(
        args.output_dir / f"{stem}.summary.json",
        (
            json.dumps(
                summary.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=True
            )
            + "\n"
        ).encode(),
    )
    print(summary_text, end="")

    if report.status != "COMPLETE":
        print("扫描报告不完整；未生成行情需求计划。")
        return 1
    plan = build_market_data_requirement_plan(report)
    plan_text = render_market_data_requirement_plan(plan)
    _write_new_or_equal(
        args.output_dir / f"{plan.plan_hash}.requirements.json",
        (
            json.dumps(
                plan.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=True
            )
            + "\n"
        ).encode(),
    )
    _write_new_or_equal(
        args.output_dir / f"{plan.plan_hash}.requirements.txt", plan_text.encode("utf-8")
    )
    print(plan_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
