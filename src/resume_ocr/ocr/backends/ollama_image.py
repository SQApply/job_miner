from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ...schemas import OcrDocument, OcrPage
from ...utils import compact_text
from ..preprocess import TempWorkDir, image_to_optimized_jpeg, render_pdf_pages_to_images
from .base import OcrBackend


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OllamaImageOcrBackend(OcrBackend):
    """Fallback backend that talks directly to Ollama /api/generate.

    PDFs are rendered page-by-page to optimized JPEGs. Images are optimized and
    sent directly as base64.
    """

    @retry(
        reraise=True,
        stop=stop_after_attempt(2),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        retry=retry_if_exception_type((requests.RequestException, TimeoutError)),
    )
    def _ocr_image(self, image_path: Path) -> str:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        payload = {
            "model": self.settings.ollama_model,
            "prompt": self.settings.prompt,
            "images": [image_b64],
            "stream": False,
            "options": {
                "temperature": 0,
                "top_p": 0.00001,
                "top_k": 1,
                "repeat_penalty": 1.1,
                "num_predict": 8192,
            },
        }
        url = self.settings.ollama_base_url.rstrip("/") + "/api/generate"
        response = requests.post(url, json=payload, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        data = response.json()
        return compact_text(data.get("response") or "")

    def _prepare_images(self, source_path: Path, work_dir: Path) -> list[Path]:
        if source_path.suffix.lower() == ".pdf":
            return render_pdf_pages_to_images(
                source_path,
                work_dir / "pages",
                max_pages=self.settings.max_pages,
                dpi=self.settings.pdf_dpi,
                max_dimension=self.settings.max_image_dimension,
                jpeg_quality=self.settings.jpeg_quality,
            )
        return [
            image_to_optimized_jpeg(
                source_path,
                work_dir / "images",
                max_dimension=self.settings.max_image_dimension,
                jpeg_quality=self.settings.jpeg_quality,
            )
        ]

    def parse(self, source_path: Path, *, resume_id: str, sha256: str) -> OcrDocument:
        started = _utc_now()
        t0 = time.perf_counter()
        pages: list[OcrPage] = []
        raw: list[dict[str, Any]] = []

        with TempWorkDir(prefix="resume_ocr_ollama_") as work_dir:
            image_paths = self._prepare_images(source_path, work_dir)
            for index, image_path in enumerate(image_paths, start=1):
                page_t0 = time.perf_counter()
                markdown = self._ocr_image(image_path)
                elapsed = round(time.perf_counter() - page_t0, 3)
                pages.append(
                    OcrPage(
                        page_number=index,
                        markdown=markdown,
                        image_path=str(image_path),
                        elapsed_seconds=elapsed,
                    )
                )
                raw.append({"page_number": index, "markdown": markdown, "elapsed_seconds": elapsed})

        combined_markdown = compact_text("\n\n---\n\n".join(page.markdown for page in pages))
        completed = _utc_now()
        return OcrDocument(
            source_path=str(source_path),
            resume_id=resume_id,
            sha256=sha256,
            file_name=source_path.name,
            file_ext=source_path.suffix.lower(),
            backend="ollama_image",
            used_native_text=False,
            markdown=combined_markdown,
            pages=pages,
            raw_result=raw,
            started_at=started,
            completed_at=completed,
            elapsed_seconds=round(time.perf_counter() - t0, 3),
        )
