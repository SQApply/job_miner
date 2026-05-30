from __future__ import annotations

from pathlib import Path

from .common.env import load_runtime_env


def resolve_root(root: str | Path | None = None) -> Path:
    base = Path(root or ".").resolve()
    load_runtime_env(base)
    return base