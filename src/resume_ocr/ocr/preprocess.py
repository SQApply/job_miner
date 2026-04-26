from __future__ import annotations

import tempfile
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber
from PIL import Image, ImageOps

from ..utils import compact_text, ensure_dir


def extract_native_pdf_text(path: Path, max_pages: int) -> str:
    """Extract embedded text from a text-based PDF.

    This is deliberately conservative. If this produces enough text, the pipeline
    can skip expensive OCR for text-native resumes.
    """
    chunks: list[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages[:max_pages]:
                chunks.append(page.extract_text() or "")
    except Exception:
        return ""
    return compact_text("\n\n".join(chunks))


def render_pdf_pages_to_images(
    path: Path,
    output_dir: Path,
    *,
    max_pages: int,
    dpi: int,
    max_dimension: int,
    jpeg_quality: int,
) -> list[Path]:
    ensure_dir(output_dir)
    image_paths: list[Path] = []
    doc = fitz.open(path)
    try:
        page_count = min(len(doc), max_pages)
        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        for index in range(page_count):
            page = doc.load_page(index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image_path = output_dir / f"page_{index + 1:03d}.jpg"
            pix.save(str(image_path))
            optimize_image(image_path, image_path, max_dimension=max_dimension, jpeg_quality=jpeg_quality)
            image_paths.append(image_path)
    finally:
        doc.close()
    return image_paths


def optimize_image(input_path: Path, output_path: Path, *, max_dimension: int, jpeg_quality: int) -> Path:
    with Image.open(input_path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        if max(img.size) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path, format="JPEG", quality=jpeg_quality, optimize=True)
    return output_path


def image_to_optimized_jpeg(
    path: Path,
    output_dir: Path,
    *,
    max_dimension: int,
    jpeg_quality: int,
) -> Path:
    ensure_dir(output_dir)
    output_path = output_dir / f"{path.stem}.jpg"
    return optimize_image(path, output_path, max_dimension=max_dimension, jpeg_quality=jpeg_quality)


class TempWorkDir:
    def __init__(self, prefix: str = "resume_ocr_"):
        self.prefix = prefix
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix=self.prefix)
        self.path = Path(self._tmp.name)
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
