from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..schemas import LlmSettings, ResumeProfile
from ..utils import safe_truncate


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> Any:
    text = _strip_code_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


class ResumeExtractor:
    def __init__(self, settings: LlmSettings, *, max_markdown_chars: int):
        self.settings = settings
        self.max_markdown_chars = max_markdown_chars

    def _build_prompt(self, markdown: str, *, resume_id: str, file_name: str, sha256: str) -> str:
        schema = ResumeProfile.model_json_schema()
        markdown = safe_truncate(markdown, self.max_markdown_chars)
        return f"""
You are an enterprise resume parsing system for a job recommendation platform.
Extract structured data from the OCR Markdown.

Rules:
1. Return ONLY valid JSON. No markdown fences. No explanations.
2. Use null when a field is not present.
3. Do not invent employers, dates, degrees, skills, or experience.
4. Normalize skills into short canonical phrases, for example: Python, FastAPI, PyTorch, Azure AI Search.
5. Preserve evidence-backed values from the resume even if formatting is messy.
6. Use the exact schema keys shown below.

Required fixed values:
- resume_id: {resume_id}
- source_file_name: {file_name}
- sha256: {sha256}

JSON schema:
{json.dumps(schema, ensure_ascii=False)}

OCR Markdown:
{markdown}
""".strip()

    @retry(
        reraise=True,
        stop=stop_after_attempt(2),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        retry=retry_if_exception_type((requests.RequestException, TimeoutError)),
    )
    def _call_ollama(self, prompt: str) -> str:
        url = self.settings.base_url.rstrip("/") + "/api/generate"
        payload = {
            "model": self.settings.provider,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": self.settings.temperature,
                "num_predict": self.settings.max_tokens,
            },
        }
        response = requests.post(url, json=payload, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        data = response.json()
        return data.get("response") or ""

    def extract(self, markdown: str, *, resume_id: str, file_name: str, sha256: str, ocr_markdown_path: str | None) -> ResumeProfile:
        prompt = self._build_prompt(markdown, resume_id=resume_id, file_name=file_name, sha256=sha256)
        response_text = self._call_ollama(prompt)
        payload = _extract_json(response_text)
        if not isinstance(payload, dict):
            raise ValueError(f"Resume extraction did not return a JSON object. Raw response: {response_text[:1000]}")

        payload.setdefault("resume_id", resume_id)
        payload.setdefault("source_file_name", file_name)
        payload.setdefault("sha256", sha256)
        payload["raw_ocr_markdown_path"] = ocr_markdown_path

        try:
            return ResumeProfile.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"Resume extraction JSON failed schema validation: {exc}") from exc
