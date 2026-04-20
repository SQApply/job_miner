from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def unique_keep_order(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def normalize_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href)