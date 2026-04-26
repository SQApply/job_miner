from __future__ import annotations

from pathlib import Path

import requests

from ..schemas import SystemConfig


def check_ollama(config: SystemConfig) -> dict:
    url = config.llm.base_url.rstrip("/") + "/api/tags"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        models = [item.get("name") for item in data.get("models", [])]
        return {"ok": True, "base_url": config.llm.base_url, "models": models}
    except Exception as exc:
        return {"ok": False, "base_url": config.llm.base_url, "error": str(exc)}


def check_glmocr_config(root: Path, config: SystemConfig) -> dict:
    path = root / config.ocr.glmocr_config_path
    return {"ok": path.exists(), "path": str(path)}
