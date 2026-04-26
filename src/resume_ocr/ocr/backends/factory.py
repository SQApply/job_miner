from __future__ import annotations

from pathlib import Path

from ...schemas import OcrSettings
from .base import OcrBackend
from .glmocr_sdk import GlmOcrSdkBackend
from .ollama_image import OllamaImageOcrBackend


def build_ocr_backend(settings: OcrSettings, root: Path) -> OcrBackend:
    if settings.backend == "glmocr_sdk":
        return GlmOcrSdkBackend(settings, root)
    if settings.backend == "ollama_image":
        return OllamaImageOcrBackend(settings, root)
    raise ValueError(f"Unsupported OCR backend: {settings.backend}")
