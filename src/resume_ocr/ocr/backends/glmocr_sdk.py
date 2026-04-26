from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...schemas import OcrDocument
from ...utils import compact_text
from .base import OcrBackend


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if hasattr(value, "dict"):
        return _to_jsonable(value.dict())
    return str(value)


class GlmOcrSdkBackend(OcrBackend):
    """Official GLM-OCR SDK backend.

    It is the preferred production path because the SDK handles layout detection,
    parallel region OCR, Markdown formatting, and PDF/image loading.
    """

    def parse(self, source_path: Path, *, resume_id: str, sha256: str) -> OcrDocument:
        started = _utc_now()
        t0 = time.perf_counter()

        try:
            from glmocr import GlmOcr
        except Exception as exc:  # pragma: no cover - depends on optional runtime package
            raise RuntimeError(
                "glmocr is not installed. Install with: pip install 'glmocr[selfhosted]' "
                "or switch ocr.backend to 'ollama_image'."
            ) from exc

        config_path = self.root / self.settings.glmocr_config_path
        if not config_path.exists():
            raise FileNotFoundError(f"GLM-OCR config not found: {config_path}")

        kwargs: dict[str, Any] = {"config_path": str(config_path)}
        if self.settings.layout_device:
            kwargs["layout_device"] = self.settings.layout_device

        with GlmOcr(**kwargs) as parser:
            result = parser.parse(str(source_path))

        markdown = compact_text(getattr(result, "markdown_result", "") or "")
        raw_result = getattr(result, "json_result", None)
        if raw_result is None:
            raw_result = _to_jsonable(result)

        completed = _utc_now()
        return OcrDocument(
            source_path=str(source_path),
            resume_id=resume_id,
            sha256=sha256,
            file_name=source_path.name,
            file_ext=source_path.suffix.lower(),
            backend="glmocr_sdk",
            used_native_text=False,
            markdown=markdown,
            raw_result=_to_jsonable(raw_result),
            started_at=started,
            completed_at=completed,
            elapsed_seconds=round(time.perf_counter() - t0, 3),
        )
