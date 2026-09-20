"""Build the immutable Phase 3 Stage A candidate comparison artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.dev_source_scan import validate_execution_checkpoint
from slagalpha.research.rescan_stage_a import (
    BASELINE_CANDIDATE_HASH,
    StageAFunnelArtifact,
)
from slagalpha.research.scan_plan import model_hash

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = (
    "d889a882a5f551931d741752b28b65e1779cc3d247729abff249a221d6b976c4",
    "def218e7a145e94ae8c08f2a601f337e1f53f871e7bda96339514af9f8c3a706",
    "15ca63f9a27f254d6cc0117b586e58399d500893ccd263ac6729cc09752f064f",
)
NAMES = {
    BASELINE_CANDIDATE_HASH: "baseline",
    CANDIDATES[0]: "pivot_window_3x3",
    CANDIDATES[1]: "compression_threshold_0.50",
    CANDIDATES[2]: "compression_threshold_1.00",
}
PIVOT_RIGHT = {
    BASELINE_CANDIDATE_HASH: 2,
    CANDIDATES[0]: 3,
    CANDIDATES[1]: 2,
    CANDIDATES[2]: 2,
}
INTERVAL_MINUTES = {"15m": 15, "1h": 60}


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _load_artifact(root: Path, candidate_hash: str) -> tuple[StageAFunnelArtifact, Path]:
    paths = sorted((root / candidate_hash[:12]).glob("*.json"))
    complete = []
    for path in paths:
        artifact = StageAFunnelArtifact.model_validate_json(path.read_bytes())
        if artifact.candidate_hash == candidate_hash and artifact.status == "COMPLETE":
            complete.append((artifact, path))
    if len(complete) != 1:
        raise RuntimeError(
            f"expected one complete artifact for {candidate_hash}, got {len(complete)}"
        )
    return complete[0]


def _checkpoint_documents(
    project_dir: Path, artifact: StageAFunnelArtifact,
) -> tuple[dict[str, Any], ...]:
    root = (
        project_dir / "data" / "checkpoints" / "p9_stage_a"
        / artifact.candidate_hash / model_hash(artifact.execution_binding)
    )
    documents = []
    for path in sorted(root.glob("*.json")):
        value = json.loads(path.read_bytes())
        validate_execution_checkpoint(value, artifact.execution_binding)
        documents.append(value)
    if len(documents) != len(artifact.scanned_symbols):
        raise RuntimeError(f"checkpoint count mismatch for {artifact.candidate_hash}")
    if tuple(sorted(item["symbol"] for item in documents)) != artifact.scanned_symbols:
        raise RuntimeError(f"checkpoint symbols mismatch for {artifact.candidate_hash}")
    return tuple(documents)


def _accepted_ids(artifact: StageAFunnelArtifact) -> set[str]:
    return set(artifact.accepted_logical_signal_ids)


def _trigger_rows(documents: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [row for document in documents for row in document["confirmed_triggers"]]


def _top_delta(
    baseline: Counter[str], candidate: Counter[str], *, limit: int = 15,
) -> list[dict[str, Any]]:
    keys = set(baseline) | set(candidate)
    rows: list[dict[str, int | str]] = [
        {
            "key": key,
            "baseline": baseline[key],
            "candidate": candidate[key],
            "delta": candidate[key] - baseline[key],
        }
        for key in keys
        if candidate[key] != baseline[key]
    ]
    return sorted(
        rows,
        key=lambda row: (-abs(int(row["delta"])), str(row["key"])),
    )[:limit]


def _ratio(numerator: int, denominator: int) -> str | None:
    return None if denominator == 0 else str(Decimal(numerator) / Decimal(denominator))


def _candidate_summary(
    artifact: StageAFunnelArtifact,
    documents: tuple[dict[str, Any], ...],
    baseline: StageAFunnelArtifact,
    baseline_documents: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    if artifact.funnel is None or baseline.funnel is None:
        raise RuntimeError("complete artifact lacks funnel")
    funnel = artifact.funnel
    base_funnel = baseline.funnel
    ids = _accepted_ids(artifact)
    base_ids = _accepted_ids(baseline)
    common_ids = ids & base_ids
    new_ids = ids - base_ids
    removed_ids = base_ids - ids
    triggers = _trigger_rows(documents)
    base_triggers = _trigger_rows(baseline_documents)
    by_symbol = Counter(row["symbol"] for row in triggers)
    base_by_symbol = Counter(row["symbol"] for row in base_triggers)
    setup_by_symbol = Counter({
        symbol: counts.get("setup_eligible_count", 0)
        for symbol, counts in artifact.per_symbol_counts.items()
    })
    base_setup_by_symbol = Counter({
        symbol: counts.get("setup_eligible_count", 0)
        for symbol, counts in baseline.per_symbol_counts.items()
    })
    by_date = Counter(row["confirmation_close_time"][:10] for row in triggers)
    base_by_date = Counter(row["confirmation_close_time"][:10] for row in base_triggers)
    trigger_types = Counter(row["primary_trigger"] for row in triggers)
    base_trigger_types = Counter(row["primary_trigger"] for row in base_triggers)
    pivot_counts: Counter[str] = Counter()
    zone_observations: Counter[str] = Counter()
    for document in documents:
        pivot_counts.update(document["diagnostics"]["pivot_counts"])
        zone_observations.update(document["diagnostics"]["zone_observation_counts"])
    base_pivots: Counter[str] = Counter()
    base_zones: Counter[str] = Counter()
    for document in baseline_documents:
        base_pivots.update(document["diagnostics"]["pivot_counts"])
        base_zones.update(document["diagnostics"]["zone_observation_counts"])
    return {
        "name": NAMES[artifact.candidate_hash],
        "candidate_hash": artifact.candidate_hash,
        "status": artifact.status,
        "phase_3_gate": artifact.phase_3_gate,
        "threshold_status": artifact.threshold_evaluation["status"],
        "artifact_hash": artifact.artifact_hash,
        "scanner_report_hash": artifact.scanner_report_hash,
        "accepted_request_set_hash": artifact.accepted_request_set_hash,
        "funnel": funnel.model_dump(mode="json"),
        "scanned_symbols": len(artifact.scanned_symbols),
        "accepted_symbol_count": len(artifact.accepted_symbols),
        "accepted_symbols": list(artifact.accepted_symbols),
        "rates": {
            "setup_eligible_over_evaluated": _ratio(
                funnel.setup_eligible_count, funnel.setup_evaluated_count
            ),
            "confirmed_over_setup_eligible": _ratio(
                funnel.trigger_confirmed_count, funnel.setup_eligible_count
            ),
            "accepted_over_confirmed": _ratio(
                funnel.accepted_count, funnel.trigger_confirmed_count
            ),
        },
        "baseline_comparison": {
            "setup_eligible_delta": (
                funnel.setup_eligible_count - base_funnel.setup_eligible_count
            ),
            "confirmed_trigger_delta": (
                funnel.trigger_confirmed_count - base_funnel.trigger_confirmed_count
            ),
            "accepted_delta": funnel.accepted_count - base_funnel.accepted_count,
            "common_accepted_count": len(common_ids),
            "newly_accepted_count": len(new_ids),
            "removed_accepted_count": len(removed_ids),
            "common_accepted": sorted(common_ids),
            "newly_accepted": sorted(new_ids),
            "removed_accepted": sorted(removed_ids),
        },
        "structure_diagnostics": {
            "pivot_counts": dict(sorted(pivot_counts.items())),
            "baseline_pivot_counts": dict(sorted(base_pivots.items())),
            "pivot_count_delta": {
                key: pivot_counts[key] - base_pivots[key]
                for key in sorted(set(pivot_counts) | set(base_pivots))
            },
            "pivot_confirmation_latency_bars_distribution": {
                key: {
                    str(PIVOT_RIGHT[artifact.candidate_hash]): pivot_counts[key]
                }
                for key in sorted(pivot_counts)
            },
            "baseline_pivot_confirmation_latency_bars_distribution": {
                key: {
                    str(PIVOT_RIGHT[BASELINE_CANDIDATE_HASH]): base_pivots[key]
                }
                for key in sorted(base_pivots)
            },
            "pivot_confirmation_nominal_minutes": {
                key: PIVOT_RIGHT[artifact.candidate_hash] * INTERVAL_MINUTES[key]
                for key in sorted(pivot_counts)
            },
            "baseline_pivot_confirmation_nominal_minutes": {
                key: PIVOT_RIGHT[BASELINE_CANDIDATE_HASH] * INTERVAL_MINUTES[key]
                for key in sorted(base_pivots)
            },
            "pivot_confirmation_nominal_delta_minutes": {
                key: (
                    PIVOT_RIGHT[artifact.candidate_hash]
                    - PIVOT_RIGHT[BASELINE_CANDIDATE_HASH]
                ) * INTERVAL_MINUTES[key]
                for key in sorted(pivot_counts)
            },
            "zone_observation_counts": dict(sorted(zone_observations.items())),
            "zone_count_semantics": "SUM_OF_VISIBLE_ZONES_AT_TRIGGER_EVALUATED_SLOTS",
            "baseline_zone_observation_counts": dict(sorted(base_zones.items())),
            "zone_observation_delta": {
                key: zone_observations[key] - base_zones[key]
                for key in sorted(set(zone_observations) | set(base_zones))
            },
            "trigger_type_counts": dict(sorted(trigger_types.items())),
            "baseline_trigger_type_counts": dict(sorted(base_trigger_types.items())),
            "trigger_type_delta": {
                key: trigger_types[key] - base_trigger_types[key]
                for key in sorted(set(trigger_types) | set(base_trigger_types))
            },
            "top_symbol_setup_eligible_deltas": _top_delta(
                base_setup_by_symbol, setup_by_symbol
            ),
            "top_symbol_trigger_deltas": _top_delta(base_by_symbol, by_symbol),
            "top_date_trigger_deltas": _top_delta(base_by_date, by_date),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-root", type=Path,
        default=ROOT / "artifacts" / "sens_p3_stage_a",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "artifacts" / "sens_p3_stage_a_summary",
    )
    args = parser.parse_args(argv)
    baseline, baseline_path = _load_artifact(args.artifact_root, BASELINE_CANDIDATE_HASH)
    baseline_documents = _checkpoint_documents(ROOT, baseline)
    summaries = []
    source_paths = {"baseline": baseline_path.relative_to(ROOT).as_posix()}
    for candidate_hash in CANDIDATES:
        artifact, path = _load_artifact(args.artifact_root, candidate_hash)
        documents = _checkpoint_documents(ROOT, artifact)
        summaries.append(_candidate_summary(
            artifact, documents, baseline, baseline_documents
        ))
        source_paths[NAMES[candidate_hash]] = path.relative_to(ROOT).as_posix()
    payload = {
        "schema_version": "p9-phase3-stage-a-summary/0.1.0",
        "scope": "RESCAN_FUNNEL_ACCEPTED_REQUEST_SET_DIAGNOSTICS_ONLY",
        "stage_b_authorized": False,
        "download_started": False,
        "replay_executed": False,
        "validation_consumed": False,
        "locked_test_consumed": False,
        "baseline_artifact_hash": baseline.artifact_hash,
        "source_artifacts": source_paths,
        "candidates": summaries,
    }
    summary = {**payload, "summary_hash": _hash(payload)}
    destination = args.output_dir / f"{summary['summary_hash']}.json"
    content = canonical_json_bytes(summary)
    if destination.exists() and destination.read_bytes() != content:
        raise RuntimeError(f"immutable summary changed: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        _publish_immutable(destination, content)
    print(json.dumps(
        {"summary_hash": summary["summary_hash"], "path": str(destination)},
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
