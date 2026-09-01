"""Exact-version environment lock tests; all package sets below are explicit fixtures."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from slagalpha.reporting.environment import EnvironmentLockError, verify_environment_lock

RUNTIME = {
    "python-version": "3.12.13", "python-implementation": "CPython",
    "sys-platform": "win32", "machine": "AMD64",
}
HEADER = "".join(f"# {name}: {value}\n" for name, value in RUNTIME.items())
LOCK = (HEADER + "fixture-lib==1.0.0\nfixture-two==2.0.0\n").encode()
PACKAGES = {"Fixture_Lib": "1.0.0", "fixture.two": "2.0.0", "slagalpha": "0.1.0"}


def test_exact_pins_normalize_package_names_but_never_include_editable_source_paths() -> None:
    digest = verify_environment_lock(LOCK, runtime=RUNTIME, installed_versions=PACKAGES)
    assert digest == hashlib.sha256(LOCK).hexdigest()


@pytest.mark.parametrize("packages", [
    {**PACKAGES, "Fixture_Lib": "1.0.1"},
    {**PACKAGES, "untracked-package": "0.1.0"},
    {"Fixture_Lib": "1.0.0"},
    {**PACKAGES, "fixture-lib": "9.0.0"},
])
def test_changed_missing_extra_or_ambiguous_distributions_are_rejected(
    packages: dict[str, str],
) -> None:
    with pytest.raises(EnvironmentLockError):
        verify_environment_lock(LOCK, runtime=RUNTIME, installed_versions=packages)


@pytest.mark.parametrize("tail", [
    "fixture-lib>=1.0", "fixture-lib==1.*", "-e .", "--index-url https://example.invalid",
    "fixture-lib @ https://example.invalid/a.whl", "fixture-lib==1.0; sys_platform=='win32'",
    "fixture-lib==1.0\nfixture-lib==1.0", "slagalpha==0.1.0", "",
])
def test_unpinned_remote_or_ambiguous_lock_content_is_rejected(tail: str) -> None:
    with pytest.raises(EnvironmentLockError):
        verify_environment_lock(
            (HEADER + tail).encode(), runtime=RUNTIME, installed_versions=PACKAGES
        )


@pytest.mark.parametrize("updates", [
    {"python-version": "3.13.0"}, {"python-implementation": "PyPy"},
    {"sys-platform": "linux"}, {"machine": "ARM64"},
])
def test_runtime_must_match_captured_environment(updates: dict[str, Any]) -> None:
    with pytest.raises(EnvironmentLockError, match="runtime"):
        verify_environment_lock(LOCK, runtime={**RUNTIME, **updates}, installed_versions=PACKAGES)


@pytest.mark.parametrize("raw", [
    b"fixture-lib==1.0\n", b"\xff", LOCK + b"# python-version: 3.12.13\n",
])
def test_missing_duplicate_or_invalid_metadata_fails_closed(raw: bytes) -> None:
    with pytest.raises(EnvironmentLockError):
        verify_environment_lock(raw, runtime=RUNTIME, installed_versions=PACKAGES)
