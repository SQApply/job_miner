from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .schemas import SystemConfig


def resolve_root(root: str | Path | None = None) -> Path:
    base = Path(root or ".").resolve()
    load_dotenv(base / ".env")
    return base


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def load_system_config(root: Path) -> SystemConfig:
    config = SystemConfig.model_validate(
    _load_yaml(root / "blueprints" / "resume_ocr" / "system.yaml")
)

    if os.getenv("OLLAMA_BASE_URL"):
        config.ocr.ollama_base_url = os.environ["OLLAMA_BASE_URL"]
        config.llm.base_url = os.environ["OLLAMA_BASE_URL"]
    if os.getenv("OLLAMA_OCR_MODEL"):
        config.ocr.ollama_model = os.environ["OLLAMA_OCR_MODEL"]
    if os.getenv("OLLAMA_EXTRACT_MODEL"):
        config.llm.provider = os.environ["OLLAMA_EXTRACT_MODEL"]
    if os.getenv("RESUME_OCR_BACKEND"):
        config.ocr.backend = os.environ["RESUME_OCR_BACKEND"]
    if os.getenv("GLMOCR_LAYOUT_DEVICE"):
        config.ocr.layout_device = os.environ["GLMOCR_LAYOUT_DEVICE"]

    return config
