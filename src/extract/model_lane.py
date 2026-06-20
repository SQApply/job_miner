from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from ..schemas import JobPosting, LLMSettings


def build_llm_strategy(llm_settings: LLMSettings, instruction: str):
    from crawl4ai import LLMConfig, LLMExtractionStrategy

    return LLMExtractionStrategy(
        llm_config=LLMConfig(
            provider=llm_settings.provider,
            api_token=llm_settings.api_token,
            base_url=llm_settings.base_url,
        ),
        schema=JobPosting.model_json_schema(),
        extraction_type="schema",
        instruction=instruction,
        apply_chunking=False,
        input_format="markdown",
        extra_args={
            "temperature": llm_settings.temperature,
            "max_tokens": llm_settings.max_tokens,
        },
        verbose=False,
    )


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

    match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

    return None


def _normalize_payload(payload: dict[str, Any], fallback_url: str) -> dict[str, Any]:
    payload = dict(payload)

    payload.setdefault("job_url", fallback_url)

    if "location" in payload and "location_text" not in payload:
        payload["location_text"] = payload["location"]

    if "employmentType" in payload and "employment_type" not in payload:
        payload["employment_type"] = payload["employmentType"]

    if "applyUrl" in payload and "apply_url" not in payload:
        payload["apply_url"] = payload["applyUrl"]

    for alias in ("posted_at", "postedAt", "postedDate", "date_posted", "datePosted", "published_at", "publishedAt", "published_date", "publishedDate", "posting_date", "postingDate", "posted"):
        if alias in payload and "posted_date" not in payload:
            payload["posted_date"] = payload[alias]
            break

    for list_field in ("responsibilities", "required_skills", "preferred_skills"):
        value = payload.get(list_field)

        if value is None:
            payload[list_field] = []
        elif isinstance(value, str):
            payload[list_field] = [value]
        elif not isinstance(value, list):
            payload[list_field] = []

    return payload

def _score_candidate(payload: dict[str, Any]) -> int:
    score = 0
    if payload.get("title"):
        score += 5
    if payload.get("job_url"):
        score += 3
    if payload.get("summary"):
        score += 2
    if payload.get("location_text"):
        score += 1
    if payload.get("employment_type"):
        score += 1
    if payload.get("responsibilities"):
        score += 2
    if payload.get("required_skills"):
        score += 2
    return score


def _select_best_payload(payload: Any, fallback_url: str) -> dict[str, Any] | None:
    if payload is None:
        return None

    if isinstance(payload, dict):
        for key in ("job", "item", "record", "data"):
            if isinstance(payload.get(key), dict):
                payload = payload[key]
                break

        for key in ("jobs", "items", "records", "data"):
            if isinstance(payload.get(key), list) and payload[key]:
                payload = payload[key]
                break

    if isinstance(payload, list):
        candidates: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                candidates.append(_normalize_payload(item, fallback_url))
        if not candidates:
            return None
        return max(candidates, key=_score_candidate)

    if isinstance(payload, dict):
        return _normalize_payload(payload, fallback_url)

    return None


def parse_extracted_jobs(raw_content: Any, fallback_url: str) -> JobPosting | None:
    if raw_content is None:
        return None

    payload = raw_content
    if isinstance(raw_content, str):
        payload = _extract_json(raw_content)

    payload = _select_best_payload(payload, fallback_url)
    if payload is None:
        return None

    try:
        return JobPosting.model_validate(payload)
    except ValidationError:
        return None