"""Check exact local dependency versions without pip/network calls or editable URLs."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import re
import sys
from collections.abc import Mapping
from pathlib import Path

_RUNTIME_FIELDS = {"python-version", "python-implementation", "sys-platform", "machine"}
_PIN = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)==([0-9][A-Za-z0-9.!+]*)")


class EnvironmentLockError(ValueError):
    """The provided exact-version lock is invalid or differs from the current runtime."""


def _normalize_packages(installed: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, version in installed.items():
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key == "slagalpha":
            continue  # The source project is bound separately to a Git commit.
        if _PIN.fullmatch(f"{key}=={version}") is None:
            raise EnvironmentLockError("invalid installed distribution metadata")
        if key in normalized and normalized[key] != version:
            raise EnvironmentLockError(f"ambiguous installed distribution: {key}")
        normalized[key] = version
    return normalized


def verify_environment_lock(
    content: bytes, *, runtime: Mapping[str, str], installed_versions: Mapping[str, str],
) -> str:
    """Return the lock's byte hash only if platform and all installed pins match exactly."""

    metadata: dict[str, str] = {}
    pins: dict[str, str] = {}
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise EnvironmentLockError("environment lock must use UTF-8") from error
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            key, separator, value = line.removeprefix("#").strip().partition(":")
            if key in _RUNTIME_FIELDS and separator:
                if key in metadata or not value.strip():
                    raise EnvironmentLockError("duplicate or empty runtime field")
                metadata[key] = value.strip()
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise EnvironmentLockError("lock requires exact pins; URLs/options/ranges are refused")
        name, version = match.groups()
        if name == "slagalpha" or name in pins:
            raise EnvironmentLockError("duplicate pin or source project in environment lock")
        pins[name] = version
    if set(metadata) != _RUNTIME_FIELDS or not pins:
        raise EnvironmentLockError("environment lock requires runtime metadata and dependency pins")
    if metadata != dict(runtime):
        raise EnvironmentLockError("environment lock runtime does not match Python/platform")
    actual = _normalize_packages(installed_versions)
    if pins != actual:
        names = sorted(
            name for name in pins.keys() | actual.keys() if pins.get(name) != actual.get(name)
        )
        raise EnvironmentLockError("environment lock dependency mismatch: " + ", ".join(names))
    return hashlib.sha256(content).hexdigest()


def inspect_environment_lock(path: Path) -> str:
    """Inspect installed name/version metadata only; never read direct URLs or install packages."""

    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        if name in installed and installed[name] != distribution.version:
            raise EnvironmentLockError("conflicting installed distribution versions")
        installed[name] = distribution.version
    return verify_environment_lock(
        path.read_bytes(),
        runtime={
            "python-version": platform.python_version(),
            "python-implementation": platform.python_implementation(),
            "sys-platform": sys.platform,
            "machine": platform.machine(),
        },
        installed_versions=installed,
    )
