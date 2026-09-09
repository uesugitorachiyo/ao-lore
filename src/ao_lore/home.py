"""Standalone AO Lore repository and runtime-home resolution."""

from __future__ import annotations

import os
from pathlib import Path

from ._strict_io import ContractError, ensure_contained


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_home() -> Path:
    configured = os.environ.get("AO_LORE_HOME")
    candidate = Path(configured) if configured else repository_root() / ".ao-lore"
    absolute, _ = ensure_contained(candidate, repository_root(), "AO_LORE_HOME")
    return absolute


def require_runtime_output(path: str | os.PathLike[str]) -> Path:
    output, _ = ensure_contained(Path(path), runtime_home(), "runtime output")
    return output
