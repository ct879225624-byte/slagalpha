"""Candidate-bound Phase 3 Stage A execution and baseline gate."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from slagalpha.domain.universe import ContractRegistry, UniverseSnapshot
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.dev_source_scan import (
    _ALGORITHM_VERSION,
    ConfirmedTriggerLedger,
    ConfirmedTriggerRecord,
    DevScanFunnel,
    DevSourceScanReport,
    ScanExecutionBinding,
    _load_symbol_sources,
    _read_symbol_checkpoint,
    _source_inventory,
    build_dev_source_scan_report,
    source_catalog_hash,
)
from slagalpha.research.parameters import DevParameterVersion, require_parameter_plan_binding
from slagalpha.research.scan_plan import DevScanPlan, model_hash
from slagalpha.research.sensitivity import SensitivityPlan
from slagalpha.research.splits import ResearchSplitManifest

EXECUTOR_VERSION: Literal["p9-rescan-stage-a/0.1.0"] = "p9-rescan-stage-a/0.1.0"
BASELINE_CANDIDATE_HASH = "e7af960eec91b8e8b8abf5484f3ef98aab73a90660121714746c3b24408d49c5"
BASELINE_REPORT_HASH = "73a60b3896836188bc20e005c6123fd03ddb9aca092b5da6dd57e575308b48e2"
FINAL_LEDGER_HASH = "aa4d993bd3a8a039438c57d87f32e8d72c25b48d32c8a8a87caa5d8bc6f44f59"
TRUSTED_CHECKPOINT_BINDING = "3239205b07801da5a1296daa76f5de92fa35d091410713d45e8a57bad57bd783"
BASELINE_COUNTS = {
    "scanned_symbols": 248, "scan_slot_count": 1578210,
    "setup_evaluated_count": 1511267, "setup_eligible_count": 112275,
    "trigger_evaluated_count": 112275, "trigger_confirmed_count": 2946,
    "entry_stop_rejected_count": 2294, "take_profit_rejected_count": 625,
    "request_boundary_rejected_count": 0, "accepted_count": 27,
    "accepted_symbols": 22,
}
BASELINE_REQUEST_SET_HASH = "348a31ee3394b76d5d60a69a3b6d6ed07c4cc6d358106c5a3611b166bcee2723"


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def source_code_hash(project_dir: Path) -> str:
    paths = sorted((project_dir / "src" / "slagalpha").rglob("*.py"))
    paths += [
        project_dir / "scripts" / "p9_dev_source_scan.py",
        project_dir / "scripts" / "p9_sensitivity_phase3_stage_a.py",
    ]
    return _hash({path.relative_to(project_dir).as_posix(): hashlib.sha256(
        path.read_bytes()).hexdigest() for path in paths})


def threshold_policy() -> dict[str, Any]:
    """Fixed before candidate execution; ratios are inclusive manual-review bands."""
    return {
        "schema_version": "p9-stage-a-threshold-policy/0.1.0",
        "baseline": BASELINE_COUNTS | {
            "accepted_conversion_rate": "0.009164969450101832993890020367",
        },
        "bands": {
            "setup_eligible_count": ["0.50", "2.00"],
            "trigger_confirmed_count": ["0.3333333333333333333333333333", "3.00"],
            "accepted_count": ["0.20", "5.00"],
            "accepted_conversion_rate": ["0.20", "5.00"],
        },
        "out_of_band_action": "MANUAL_REVIEW_AND_FAIL_CLOSED",
    }


class StageAFunnelArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["p9-stage-a-funnel/0.1.0"] = "p9-stage-a-funnel/0.1.0"
    status: Literal["COMPLETE", "FAILED"]
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_binding: ScanExecutionBinding
    threshold_policy: dict[str, Any]
    threshold_evaluation: dict[str, Any]
    scanner_report_hash: str | None
    trusted_baseline_report_hash: str
    final_ledger_hash: str
    funnel: DevScanFunnel | None
    scanned_symbols: tuple[str, ...]
    accepted_symbols: tuple[str, ...]
    accepted_logical_signal_ids: tuple[str, ...]
    accepted_request_set_hash: str | None
    per_symbol_counts: dict[str, dict[str, int]]
    baseline_equivalence: bool
    phase_3_gate: Literal["OPEN_FOR_CANDIDATE_STAGE_A", "BLOCKED"]
    failure: str | None
    artifact_hash: str

    @model_validator(mode="after")
    def check(self) -> StageAFunnelArtifact:
        payload = self.model_dump(mode="json", exclude={"artifact_hash"})
        if self.artifact_hash != _hash(payload):
            raise ValueError("Stage A artifact hash mismatch")
        if self.status == "COMPLETE" and self.funnel is None:
            raise ValueError("complete Stage A artifact lacks funnel")
        if self.phase_3_gate == "OPEN_FOR_CANDIDATE_STAGE_A" and not self.baseline_equivalence:
            raise ValueError("Stage A gate cannot open after a failed baseline gate")
        return self


class StageAExecutorError(RuntimeError):
    pass


def _binding(
    *, project_dir: Path, plan: SensitivityPlan, parameter: DevParameterVersion,
    scan_plan: DevScanPlan, split: ResearchSplitManifest, registry: ContractRegistry,
    snapshots: tuple[UniverseSnapshot, ...], source_hash: str,
    source_catalog_hashes: dict[str, str], environment_lock_hash: str,
) -> ScanExecutionBinding:
    symbols = tuple(sorted(source_catalog_hashes))
    binding = ScanExecutionBinding(
        executor_version=EXECUTOR_VERSION,
        scanner_algorithm_version=_ALGORITHM_VERSION,
        candidate_hash=parameter.candidate.candidate_hash,
        parameter_content_hash=parameter.content_hash,
        sensitivity_plan_hash=plan.plan_hash,
        scan_plan_hash=scan_plan.plan_hash,
        split_hash=split.split_hash,
        registry_hash=model_hash(registry),
        source_inventory_hash=source_hash,
        universe_hash=scan_plan.source_snapshot_content_hash,
        source_code_hash=source_code_hash(project_dir),
        environment_lock_hash=environment_lock_hash,
        threshold_policy_hash=_hash(threshold_policy()),
        source_catalog_hashes={symbol: source_catalog_hashes[symbol] for symbol in symbols},
    )
    return binding


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise StageAExecutorError(f"expected JSON object: {path}")
    return value


def _write_artifact(path: Path, artifact: StageAFunnelArtifact) -> Path:
    content = canonical_json_bytes(artifact.model_dump(mode="json"))
    if path.exists() and (path.is_symlink() or path.read_bytes() != content):
        raise StageAExecutorError(f"immutable Stage A artifact changed: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _publish_immutable(path, content)
    return path


def _per_symbol(payload: dict[str, Any]) -> dict[str, int]:
    counts = dict(payload["outcome_counts"])
    counts["scan_slot_count"] = int(payload["symbol_slot_count"])
    return dict(sorted((key, int(value)) for key, value in counts.items()))


def _candidate_artifact(
    *, status: Literal["COMPLETE", "FAILED"], binding: ScanExecutionBinding,
    report: DevSourceScanReport | None, symbol_payloads: dict[str, dict[str, Any]],
    trusted: DevSourceScanReport, ledger_hash: str, baseline_equivalence: bool,
    threshold_evaluation: dict[str, Any], gate_open: bool, failure: str | None,
) -> StageAFunnelArtifact:
    accepted = () if report is None else report.accepted_requests
    funnel = None if report is None else report.funnel
    payload = {
        "schema_version": "p9-stage-a-funnel/0.1.0", "status": status,
        "candidate_hash": binding.candidate_hash,
        "parameter_content_hash": binding.parameter_content_hash,
        "execution_binding": binding.model_dump(mode="json"),
        "threshold_policy": threshold_policy(),
        "threshold_evaluation": threshold_evaluation,
        "scanner_report_hash": None if report is None else report.report_hash,
        "trusted_baseline_report_hash": trusted.report_hash,
        "final_ledger_hash": ledger_hash,
        "funnel": None if funnel is None else funnel.model_dump(mode="json"),
        "scanned_symbols": () if report is None else report.scanned_symbols,
        "accepted_symbols": () if report is None else report.accepted_symbols,
        "accepted_logical_signal_ids": tuple(sorted(
            item.request.request.armed.logical_signal_id for item in accepted)),
        "accepted_request_set_hash": None if report is None else report.request_set_hash,
        "per_symbol_counts": {key: _per_symbol(value)
                              for key, value in sorted(symbol_payloads.items())},
        "baseline_equivalence": baseline_equivalence,
        "phase_3_gate": "OPEN_FOR_CANDIDATE_STAGE_A" if gate_open else "BLOCKED",
        "failure": failure,
    }
    return StageAFunnelArtifact.model_validate({**payload, "artifact_hash": _hash(payload)})


def baseline_equivalence_check(
    *, report: DevSourceScanReport, trusted_report: DevSourceScanReport,
    records: tuple[ConfirmedTriggerRecord, ...], final_ledger: ConfirmedTriggerLedger,
) -> bool:
    """Compare the complete regenerated Stage A result with frozen baseline evidence."""
    trusted_ids = tuple(sorted(
        item.request.request.armed.logical_signal_id
        for item in trusted_report.accepted_requests
    ))
    observed_ids = tuple(sorted(
        item.request.request.armed.logical_signal_id
        for item in report.accepted_requests
    ))
    return (
        report.report_hash == trusted_report.report_hash
        and len(report.scanned_symbols) == BASELINE_COUNTS["scanned_symbols"]
        and report.scanned_symbols == trusted_report.scanned_symbols
        and report.funnel == trusted_report.funnel
        and all(
            getattr(report.funnel, key) == value
            for key, value in BASELINE_COUNTS.items()
            if key not in {"scanned_symbols", "accepted_symbols"}
        )
        and len(report.accepted_symbols) == BASELINE_COUNTS["accepted_symbols"]
        and report.accepted_requests == trusted_report.accepted_requests
        and report.request_set_hash == BASELINE_REQUEST_SET_HASH
        and observed_ids == trusted_ids
        and records == final_ledger.records
        and final_ledger.ledger_hash == FINAL_LEDGER_HASH
    )


def evaluate_scale_thresholds(report: DevSourceScanReport) -> dict[str, Any]:
    """Evaluate the frozen pre-candidate scale bands without binary floats."""
    policy = threshold_policy()
    observed = {
        "setup_eligible_count": Decimal(report.funnel.setup_eligible_count),
        "trigger_confirmed_count": Decimal(report.funnel.trigger_confirmed_count),
        "accepted_count": Decimal(report.funnel.accepted_count),
    }
    baseline = {
        key: Decimal(BASELINE_COUNTS[key])
        for key in observed
    }
    confirmed = Decimal(report.funnel.trigger_confirmed_count)
    observed_conversion = (
        Decimal(report.funnel.accepted_count) / confirmed if confirmed else None
    )
    baseline_conversion = Decimal(policy["baseline"]["accepted_conversion_rate"])
    ratios: dict[str, str | None] = {
        key: str(observed[key] / baseline[key]) for key in observed
    }
    ratios["accepted_conversion_rate"] = (
        None if observed_conversion is None
        else str(observed_conversion / baseline_conversion)
    )
    checks: dict[str, bool] = {}
    for key, bounds in policy["bands"].items():
        ratio = ratios[key]
        checks[key] = (
            ratio is not None and Decimal(bounds[0]) <= Decimal(ratio) <= Decimal(bounds[1])
        )
    return {
        "observed": {
            **{key: int(value) for key, value in observed.items()},
            "accepted_conversion_rate": (
                None if observed_conversion is None else str(observed_conversion)
            ),
        },
        "ratios_to_baseline": ratios,
        "within_band": checks,
        "status": "PASS" if all(checks.values()) else "MANUAL_REVIEW_REQUIRED",
    }


def _require_compatible_baseline_gate(
    baseline_gate: StageAFunnelArtifact | None,
    binding: ScanExecutionBinding,
) -> None:
    if baseline_gate is None:
        raise StageAExecutorError("candidate Stage A requires an explicit baseline gate artifact")
    if (
        baseline_gate.candidate_hash != BASELINE_CANDIDATE_HASH
        or baseline_gate.status != "COMPLETE"
        or not baseline_gate.baseline_equivalence
        or baseline_gate.phase_3_gate != "OPEN_FOR_CANDIDATE_STAGE_A"
        or baseline_gate.trusted_baseline_report_hash != BASELINE_REPORT_HASH
        or baseline_gate.final_ledger_hash != FINAL_LEDGER_HASH
    ):
        raise StageAExecutorError("baseline gate artifact is not open and trusted")
    stable_fields = (
        "executor_version", "scanner_algorithm_version", "sensitivity_plan_hash",
        "split_hash", "registry_hash", "source_inventory_hash", "universe_hash",
        "source_code_hash", "environment_lock_hash", "threshold_policy_hash",
        "source_catalog_hashes",
    )
    for field in stable_fields:
        if getattr(baseline_gate.execution_binding, field) != getattr(binding, field):
            raise StageAExecutorError(f"baseline gate binding mismatch: {field}")


def trusted_baseline_symbol_counts(project_dir: Path) -> dict[str, dict[str, int]]:
    directory = (
        project_dir / "data" / "checkpoints" / "dev_source_scan"
        / TRUSTED_CHECKPOINT_BINDING
    )
    paths = sorted(directory.glob("*.json"))
    if len(paths) != BASELINE_COUNTS["scanned_symbols"]:
        raise StageAExecutorError("trusted baseline symbol checkpoint count mismatch")
    result: dict[str, dict[str, int]] = {}
    for path in paths:
        document = _read_symbol_checkpoint(
            path, binding_hash=TRUSTED_CHECKPOINT_BINDING, symbol=path.stem
        )
        if document is None:
            raise StageAExecutorError(f"trusted baseline checkpoint missing: {path}")
        result[path.stem] = _per_symbol(document)
    return result


def execute_stage_a(
    *, project_dir: Path, plan: SensitivityPlan, parameter: DevParameterVersion,
    split: ResearchSplitManifest, scan_plan: DevScanPlan, registry: ContractRegistry,
    snapshots: tuple[UniverseSnapshot, ...], checkpoint_dir: Path,
    environment_lock_hash: str, trusted_report: DevSourceScanReport,
    final_ledger: ConfirmedTriggerLedger,
    baseline_gate: StageAFunnelArtifact | None = None,
) -> tuple[StageAFunnelArtifact, Path]:
    require_parameter_plan_binding(parameter, plan)
    is_baseline = parameter.candidate.changed_parameter == "BASELINE"
    if (is_baseline
            and parameter.content_hash != trusted_report.parameter_content_hash):
        raise StageAExecutorError("baseline parameter does not match trusted scan")
    if scan_plan.sensitivity_plan_hash != plan.plan_hash or split.split_hash != plan.split_hash:
        raise StageAExecutorError("candidate context does not match frozen plan")
    symbols = tuple(sorted({symbol for day in scan_plan.days for symbol in day.symbols}))
    source_hash, _, _ = _source_inventory(
        project_dir, symbols, final_period=scan_plan.days[-1].end_exclusive.strftime("%Y-%m")
    )
    catalog_hashes = {
        symbol: source_catalog_hash(_load_symbol_sources(
            project_dir, symbol, final_period=scan_plan.days[-1].end_exclusive.strftime("%Y-%m")
        )) for symbol in symbols
    }
    binding = _binding(
        project_dir=project_dir, plan=plan, parameter=parameter, scan_plan=scan_plan,
        split=split, registry=registry, snapshots=snapshots, source_hash=source_hash,
        source_catalog_hashes=catalog_hashes, environment_lock_hash=environment_lock_hash,
    )
    symbol_payloads: dict[str, dict[str, Any]] = {}
    records: list[ConfirmedTriggerRecord] = []
    report: DevSourceScanReport | None = None
    baseline_gate_passed = False

    def result_sink(payload: dict[str, Any]) -> None:
        symbol_payloads[payload["symbol"]] = payload
        records.extend(ConfirmedTriggerRecord.model_validate(item)
                       for item in payload["confirmed_triggers"])

    try:
        if not is_baseline:
            _require_compatible_baseline_gate(baseline_gate, binding)
            baseline_gate_passed = True
        report = build_dev_source_scan_report(
            project_dir=project_dir, scan_plan=scan_plan, split=split, plan=plan,
            parameter=parameter, snapshots=snapshots, registry=registry,
            checkpoint_dir=checkpoint_dir, execution_binding=binding,
            symbol_result_sink=result_sink,
        )
        if report.anomalies or report.status != "COMPLETE":
            raise StageAExecutorError("source anomaly produced a partial Stage A report")
        record_tuple = tuple(sorted(records, key=lambda item: (
            item.confirmation_close_time, item.symbol, item.logical_signal_id)))
        scale_evaluation = evaluate_scale_thresholds(report)
        if is_baseline:
            trusted_counts = trusted_baseline_symbol_counts(project_dir)
            observed_counts = {
                key: _per_symbol(value) for key, value in symbol_payloads.items()
            }
            if observed_counts != trusted_counts:
                raise StageAExecutorError("baseline per-symbol reconciliation mismatch")
            baseline_equivalence = baseline_equivalence_check(
                report=report, trusted_report=trusted_report,
                records=record_tuple, final_ledger=final_ledger,
            )
            if not baseline_equivalence:
                raise StageAExecutorError("BASELINE_EQUIVALENCE=false")
        else:
            baseline_equivalence = True
            if scale_evaluation["status"] != "PASS":
                raise StageAExecutorError("candidate scale threshold requires manual review")
        artifact = _candidate_artifact(
            status="COMPLETE", binding=binding, report=report,
            symbol_payloads=symbol_payloads, trusted=trusted_report,
            ledger_hash=final_ledger.ledger_hash,
            baseline_equivalence=baseline_equivalence,
            threshold_evaluation=scale_evaluation, gate_open=True, failure=None,
        )
    except Exception as error:
        artifact = _candidate_artifact(
            status="FAILED", binding=binding, report=report,
            symbol_payloads=symbol_payloads, trusted=trusted_report,
            ledger_hash=final_ledger.ledger_hash,
            baseline_equivalence=baseline_gate_passed,
            threshold_evaluation=(
                {"status": "NOT_AVAILABLE"}
                if report is None else evaluate_scale_thresholds(report)
            ),
            gate_open=False,
            failure=f"{type(error).__name__}: {error}",
        )
    path = (
        project_dir / "artifacts" / "sens_p3_stage_a"
        / binding.candidate_hash[:12] / f"{artifact.artifact_hash}.json"
    )
    return artifact, _write_artifact(path, artifact)
