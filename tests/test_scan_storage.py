"""Storage tests mock whole-day recomputation explicitly; fixtures are not real scan results."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from slagalpha.reporting.run_manifest import RunManifestError
from slagalpha.research.candle_inputs import CandleInputError
from slagalpha.research.request_set import DevScanRecord, build_dev_scan_day_evidence
from slagalpha.research.scan_storage import (
    read_declared_scan_source,
    restore_source_bound_scan_day,
    save_source_bound_scan_day,
)
from test_scan_days import sample_day as sample_day


@pytest.fixture
def storage_context(sample_day: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    source = sample_day["sources"][-1]  # Structurally valid P6 over synthetic raw files.
    day = sample_day["scan_plan"].days[0]
    records = tuple(DevScanRecord(
        symbol=symbol, confirmation_close=day.first_confirmation + timedelta(minutes=15 * index),
        outcome="NO_SIGNAL", reason_codes=("EXPLICIT_STORAGE_TEST_MOCK",),
        upstream_evidence_hash=source.content_hash,
    ) for index in range(day.time_count) for symbol in day.symbols)
    evidence = build_dev_scan_day_evidence(scan_plan=sample_day["scan_plan"],
                                         selection_date=day.selection_date, records=records)
    return {**{key: value for key, value in sample_day.items() if key != "selection_date"},
            "project_dir": tmp_path, "evidence": evidence, "sources": (source,) * len(records)}


@pytest.fixture
def revalidation_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def mock_revalidate(evidence: Any, **kwargs: Any) -> None:
        sources = tuple(kwargs["sources"])
        assert tuple(source.content_hash for source in sources) == tuple(
            record.upstream_evidence_hash for record in evidence.records
        )
        calls.append((evidence, sources))

    monkeypatch.setattr("slagalpha.research.scan_storage.require_source_bound_scan_day",
                        mock_revalidate)
    return calls


def _restore_context(context: dict[str, Any]) -> dict[str, Any]:
    return {**{key: value for key, value in context.items() if key not in ("sources", "evidence")},
            "content_hash": context["evidence"].content_hash}


def test_immutable_round_trip_always_calls_source_revalidation(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]],
) -> None:
    path = save_source_bound_scan_day(**storage_context)
    original = path.read_bytes()
    assert save_source_bound_scan_day(**storage_context) == path
    assert path.read_bytes() == original
    restored = restore_source_bound_scan_day(**_restore_context(storage_context))
    assert restored == storage_context["evidence"]
    assert len(revalidation_calls) == 3
    source = storage_context["sources"][0]
    assert read_declared_scan_source(project_dir=storage_context["project_dir"],
                                     content_hash=source.content_hash) == source
    assert restored.strategy_evidence_verified is False


def test_interrupted_source_write_is_not_a_completed_day_and_retry_is_idempotent(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import slagalpha.research.scan_storage as module

    publisher: Callable[[Path, bytes], None] = module.__dict__["_publish_immutable"]

    def interrupted(path: Path, content: bytes) -> None:
        if path.parent.name == "source_scan_day":
            raise OSError("simulated interruption before completion receipt")
        publisher(path, content)

    monkeypatch.setattr(module, "_publish_immutable", interrupted)
    with pytest.raises(OSError, match="simulated interruption"):
        save_source_bound_scan_day(**storage_context)
    with pytest.raises(FileNotFoundError):
        restore_source_bound_scan_day(**_restore_context(storage_context))
    assert len(list(storage_context["project_dir"].glob(
        "data/manifests/scan_trade_plan_source/*.json"
    ))) == 1
    monkeypatch.setattr(module, "_publish_immutable", publisher)
    save_source_bound_scan_day(**storage_context)
    assert restore_source_bound_scan_day(**_restore_context(storage_context)) == (
        storage_context["evidence"]
    )


@pytest.mark.parametrize("damage", ["source_missing", "source_changed", "day_changed"])
def test_completed_checkpoints_are_not_repaired_or_overwritten(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]], damage: str,
) -> None:
    day_path = save_source_bound_scan_day(**storage_context)
    source_path = next(storage_context["project_dir"].glob(
        "data/manifests/scan_trade_plan_source/*.json"
    ))
    if damage == "source_missing":
        source_path.unlink()  # Only the synthetic test checkpoint is removed.
    elif damage == "source_changed":
        source_path.write_bytes(b"changed synthetic source receipt")
    else:
        day_path.write_bytes(b"changed synthetic completion receipt")
    with pytest.raises(CandleInputError, match="missing sources|checkpoint changed"):
        save_source_bound_scan_day(**storage_context)
    with pytest.raises((ValueError, OSError)):
        restore_source_bound_scan_day(**_restore_context(storage_context))
    if damage == "source_missing":
        assert not source_path.exists()


def test_source_reference_mismatch_is_rejected_before_any_write(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]],
) -> None:
    storage_context["sources"] = storage_context["sources"][:-1]
    with pytest.raises(CandleInputError, match="every ordered record"):
        save_source_bound_scan_day(**storage_context)
    assert revalidation_calls == []
    assert not (storage_context["project_dir"] / "data").exists()


@pytest.mark.parametrize("part", ["root", "data", "manifests", "source_scan_day"])
@pytest.mark.parametrize("link_kind", ["is_symlink", "is_junction"])
def test_linked_path_components_cannot_be_used_for_checkpoint_writes(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch, part: str, link_kind: str,
) -> None:
    root = storage_context["project_dir"]
    linked = root if part == "root" else root / "data"
    if part in ("manifests", "source_scan_day"):
        linked /= "manifests"
    if part == "source_scan_day":
        linked /= part
    # Simulated link detection is portable on Windows without symlink creation privileges.
    original = getattr(Path, link_kind)
    monkeypatch.setattr(Path, link_kind, lambda path: path == linked or original(path))
    with pytest.raises(CandleInputError, match="symlink"):
        save_source_bound_scan_day(**storage_context)
    assert not (root / "data").exists()


@pytest.mark.parametrize("content", [b"{}\n", b'{"x":1,"x":2}'])
def test_noncanonical_or_duplicate_key_json_is_rejected(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]], content: bytes,
) -> None:
    path = save_source_bound_scan_day(**storage_context)
    path.write_bytes(content)
    with pytest.raises(RunManifestError, match="canonical|duplicate"):
        restore_source_bound_scan_day(**_restore_context(storage_context))


def test_structural_source_load_is_not_research_approval(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slagalpha.research.scan_days import require_source_bound_scan_day

    save_source_bound_scan_day(**storage_context)
    monkeypatch.setattr("slagalpha.research.scan_storage.require_source_bound_scan_day",
                        require_source_bound_scan_day)
    # Stored test declarations are self-consistent JSON, but intentionally do not match slots.
    with pytest.raises(CandleInputError, match="exact ordered slot"):
        restore_source_bound_scan_day(**_restore_context(storage_context))


@pytest.mark.parametrize("kind", ["day", "source"])
def test_valid_json_under_wrong_filename_is_rejected(
    storage_context: dict[str, Any], revalidation_calls: list[tuple[Any, ...]], kind: str,
) -> None:
    path = save_source_bound_scan_day(**storage_context)
    if kind == "source":
        path = next(storage_context["project_dir"].glob(
            "data/manifests/scan_trade_plan_source/*.json"
        ))
    path.with_name("0" * 64 + ".json").write_bytes(path.read_bytes())
    with pytest.raises(CandleInputError, match="filename identity"):
        if kind == "day":
            restore_source_bound_scan_day(**{**_restore_context(storage_context),
                                             "content_hash": "0" * 64})
        else:
            read_declared_scan_source(project_dir=storage_context["project_dir"],
                                      content_hash="0" * 64)


def test_invalid_hash_cannot_select_an_external_path(storage_context: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        read_declared_scan_source(project_dir=storage_context["project_dir"],
                                  content_hash="../../outside")


def test_verifier_failure_leaves_no_completion_receipt(
    storage_context: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise CandleInputError("simulated changed raw archive")

    monkeypatch.setattr("slagalpha.research.scan_storage.require_source_bound_scan_day", blocked)
    with pytest.raises(CandleInputError, match="changed raw"):
        save_source_bound_scan_day(**storage_context)
    assert not (storage_context["project_dir"] / "data").exists()
