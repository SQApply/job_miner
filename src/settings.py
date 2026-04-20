from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def resolve_root(root: str | Path | None = None) -> Path:
    base = Path(root or ".").resolve()
    load_dotenv(base / ".env")
    return base