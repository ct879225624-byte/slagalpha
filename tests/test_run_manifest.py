"""Run provenance tests use synthetic input references and the existing P7 fixture."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from slagalpha.backtest.costs import CostScenario
from slagalpha.reporting.run_manifest import (
    DevRunInputs,
    DevRunManifest,
    RunManifestError,
    build_dev_run_manifest,
    read_dev_run,
    write_dev_run,
)
from test_summary import canonical

START = datetime(2024, 2, 1, tzinfo=UTC)
END = START + timedelta(seconds=1)


def inputs(**updates: Any) -> DevRunInputs:
    return DevRunInputs.model_validate({
        "run_kind": "DEV_RESEARCH",
        "code_commit": "a" * 40,
        "dirty_worktree": False,
        "strategy_version": "ma-trend-pullback/0.1.0",
        "parameter_version": "parameters/0.1.0:" + "b" * 64,
        "schema_versions": ["candle/0.1.0", "research-schema/0.1.0"],
        "contract_registry_version": "fixture:rules",
        "exclusion_ledger_version": "fixture:exclusions",
        "universe_versions": ["c" * 64],
        "candle_dataset_hashes": ["d" * 64],
        "archive_manifest_hash": "e" * 64,
        "cost_model_version": "fixture:costs/0.1.0",
        "random_seed": None,
        "command_arguments": ["fixture-replay", "--dataset-role", "DEV"],
        "environment_lock_hash": "f" * 64,
        "split_hash": "1" * 64,
        "sensitivity_plan_hash": "2" * 64,
        **updates,
    })


def result() -> dict[str, Any]:
    # Real P7 code over explicitly synthetic candles; not a market backtest.
    return cast(dict[str, Any], json.loads(canonical(CostScenario.BASELINE).canonical_json))


def manifest(payload: dict[str, Any] | None = None) -> DevRunManifest:
    return build_dev_run_manifest(
        inputs(), started_at=START, finished_at=END, result=result() if payload is None else payload
    )


def test_p7_result_round_trip_binds_exact_result(tmp_path: Path) -> None:
    payload = result()
    first = manifest(payload)
    assert manifest(dict(reversed(list(payload.items())))) == first
    directory = write_dev_run(first, payload, tmp_path)
    assert directory == tmp_path / "runs" / first.run_id
    loaded, saved_result = read_dev_run(directory)
    assert loaded == first
    assert saved_result == payload
    assert write_dev_run(first, payload, tmp_path) == directory
    assert first.model_dump(mode="json")["started_at"] == "2024-02-01T00:00:00.000Z"
    assert first.dataset_role == "DEV"


@pytest.mark.parametrize("updates", [
    {"code_commit": None}, {"dirty_worktree": True}, {"code_commit": "HEAD"},
    {"code_commit": "a" * 7}, {"code_commit": "z" * 40},
    {"dirty_worktree": "false"}, {"dirty_worktree": 0},
    {"dataset_role": "LOCKED_TEST"}, {"dataset_role": "VALIDATION"},
    {"environment_lock_hash": ""}, {"archive_manifest_hash": "missing"},
    {"universe_versions": []}, {"candle_dataset_hashes": ["c" * 64, "c" * 64]},
    {"schema_versions": ["z", "a"]}, {"command_arguments": []},
    {"parameter_version": " "}, {"parameter_version": "fixture:parameters"},
    {"parameter_version": "parameters/0.1.0:not-a-hash"},
    {"random_seed": True}, {"random_seed": -1},
])
def test_incomplete_or_nonreproducible_formal_inputs_fail_closed(updates: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        inputs(**updates)


def test_missing_commit_is_allowed_only_with_explicit_nonreproducible_dev_label() -> None:
    draft = inputs(run_kind="NON_REPRODUCIBLE_DEV_RUN", code_commit=None, dirty_worktree=True)
    artifact = build_dev_run_manifest(draft, started_at=START, finished_at=END, result=result())
    assert artifact.run_kind == "NON_REPRODUCIBLE_DEV_RUN"
    assert artifact.code_commit is None
    assert artifact.dirty_worktree is True


@pytest.mark.parametrize(("start", "finish"), [
    (START.replace(tzinfo=None), END),
    (START.replace(microsecond=1), END),
    (START, START - timedelta(seconds=1)),
])
def test_invalid_run_times_are_rejected(start: datetime, finish: datetime) -> None:
    with pytest.raises(ValueError):
        build_dev_run_manifest(inputs(), started_at=start, finished_at=finish, result=result())


def test_input_or_result_change_changes_run_identity_but_audit_time_not_result_hash() -> None:
    original = manifest()
    changed_code = build_dev_run_manifest(
        inputs(code_commit="b" * 40), started_at=START, finished_at=END, result=result()
    )
    changed_time = build_dev_run_manifest(
        inputs(), started_at=START, finished_at=END + timedelta(seconds=1), result=result()
    )
    changed_result = manifest({**result(), "test_marker": "different"})
    assert len({r.run_id for r in (original, changed_code, changed_time, changed_result)}) == 4
    assert original.result_content_hash == changed_code.result_content_hash
    assert original.result_content_hash == changed_time.result_content_hash
    assert original.result_content_hash != changed_result.result_content_hash


def test_tampered_manifest_and_mismatched_result_cannot_be_written(tmp_path: Path) -> None:
    original = manifest()
    forged = original.model_copy(update={"dirty_worktree": True})
    with pytest.raises(ValueError):
        write_dev_run(forged, result(), tmp_path)
    with pytest.raises(RunManifestError, match="result hash"):
        write_dev_run(original, {"not": "the original result"}, tmp_path)
    assert not (tmp_path / "runs").exists()
    changed = original.model_dump(mode="json")
    changed["code_commit"] = "b" * 40
    with pytest.raises(ValueError, match="content hash"):
        DevRunManifest.model_validate(changed)


@pytest.mark.parametrize("filename", ["manifest.json", "result.json"])
def test_existing_files_are_never_overwritten_even_after_corruption(
    tmp_path: Path, filename: str,
) -> None:
    original = manifest()
    directory = write_dev_run(original, result(), tmp_path)
    target = directory / filename
    target.write_bytes(b"corrupted")
    with pytest.raises(RunManifestError):
        write_dev_run(original, result(), tmp_path)
    assert target.read_bytes() == b"corrupted"
    with pytest.raises((ValueError, RunManifestError)):
        read_dev_run(directory)


def test_concurrent_identical_writes_are_idempotent(tmp_path: Path) -> None:
    original = manifest()
    payload = result()
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: write_dev_run(original, payload, tmp_path), range(8)))
    assert len(set(outputs)) == 1
    assert read_dev_run(outputs[0]) == (original, payload)
    assert not list(outputs[0].glob("*.part"))


def test_interrupted_result_only_write_can_resume_but_completed_run_cannot_lose_result(
    tmp_path: Path,
) -> None:
    original = manifest()
    directory = write_dev_run(original, result(), tmp_path)
    (directory / "manifest.json").unlink()
    assert write_dev_run(original, result(), tmp_path) == directory
    (directory / "result.json").unlink()
    with pytest.raises(RunManifestError, match="missing result"):
        write_dev_run(original, result(), tmp_path)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_result_cannot_be_saved(number: float) -> None:
    with pytest.raises(ValueError):
        manifest({"invalid": number})


@pytest.mark.parametrize("payload", [[], "not an object", None])
def test_nonobject_result_cannot_create_an_unreadable_run(payload: Any) -> None:
    with pytest.raises(RunManifestError, match="JSON object"):
        build_dev_run_manifest(inputs(), started_at=START, finished_at=END, result=payload)
