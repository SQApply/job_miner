from __future__ import annotations

import json
import re
from typing import Any

import requests
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..schemas import EducationItem, ExperienceItem, LlmSettings, ProjectItem, ResumeProfile


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


def _resume_text_window(markdown: str, max_chars: int) -> str:
    """Keep the most useful parts of long resumes: beginning + ending."""
    markdown = markdown.strip()

    if len(markdown) <= max_chars:
        return markdown

    marker = "\n\n[...RESUME_TEXT_TRUNCATED_FOR_EXTRACTION...]\n\n"
    head_chars = int(max_chars * 0.75)
    tail_chars = max_chars - head_chars - len(marker)

    if tail_chars <= 0:
        return markdown[:max_chars]

    return markdown[:head_chars] + marker + markdown[-tail_chars:]


def _basic_resume_signals(markdown: str) -> dict[str, Any]:
    """Extract obvious signals to help the LLM avoid returning an empty profile."""
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    first_lines = lines[:12]

    email_match = re.search(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        markdown,
        flags=re.IGNORECASE,
    )

    phone_match = re.search(
        r"(?:(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4})",
        markdown,
    )

    return {
        "first_non_empty_lines": first_lines,
        "detected_email": email_match.group(0) if email_match else None,
        "detected_phone": phone_match.group(0) if phone_match else None,
        "text_length_chars": len(markdown),
    }


def _experience_item_has_content(item: ExperienceItem) -> bool:
    return any(
        [
            item.company,
            item.title,
            item.location,
            item.start_date,
            item.end_date,
            item.responsibilities,
            item.technologies,
        ]
    )


def _education_item_has_content(item: EducationItem) -> bool:
    return any(
        [
            item.institution,
            item.degree,
            item.field_of_study,
            item.start_date,
            item.end_date,
            item.score_or_grade,
        ]
    )


def _project_item_has_content(item: ProjectItem) -> bool:
    return any(
        [
            item.name,
            item.description,
            item.technologies,
            item.url,
        ]
    )


def _profile_has_content(profile: ResumeProfile) -> bool:
    contact = profile.contact

    return any(
        [
            contact.full_name,
            contact.email,
            contact.phone,
            contact.location,
            contact.linkedin_url,
            contact.github_url,
            contact.portfolio_url,
            profile.headline,
            profile.summary,
            profile.total_experience_years,
            profile.current_title,
            profile.current_company,
            profile.primary_skills,
            profile.secondary_skills,
            profile.tools_and_platforms,
            profile.programming_languages,
            profile.domains,
            profile.certifications,
            any(_experience_item_has_content(item) for item in profile.experience),
            any(_education_item_has_content(item) for item in profile.education),
            any(_project_item_has_content(item) for item in profile.projects),
            profile.languages,
        ]
    )


class ResumeExtractor:
    def __init__(self, settings: LlmSettings, *, max_markdown_chars: int):
        self.settings = settings
        self.max_markdown_chars = max_markdown_chars

    def _build_prompt(
        self,
        markdown: str,
        *,
        resume_id: str,
        file_name: str,
        sha256: str,
        strict_retry: bool = False,
    ) -> str:
        markdown_limit = min(self.max_markdown_chars, 14000)

        if strict_retry:
            markdown_limit = min(self.max_markdown_chars, 10000)

        resume_text = _resume_text_window(markdown, markdown_limit)
        signals = _basic_resume_signals(markdown)

        output_template = {
            "resume_id": resume_id,
            "source_file_name": file_name,
            "sha256": sha256,
            "contact": {
                "full_name": None,
                "email": None,
                "phone": None,
                "location": None,
                "linkedin_url": None,
                "github_url": None,
                "portfolio_url": None,
            },
            "headline": None,
            "summary": None,
            "total_experience_years": None,
            "current_title": None,
            "current_company": None,
            "primary_skills": [],
            "secondary_skills": [],
            "tools_and_platforms": [],
            "programming_languages": [],
            "domains": [],
            "certifications": [],
            "experience": [],
            "education": [],
            "projects": [],
            "languages": [],
            "raw_ocr_markdown_path": None,
            "parse_warnings": [],
        }

        retry_instruction = ""
        if strict_retry:
            retry_instruction = """
STRICT RETRY MODE:
Your previous extraction was empty or invalid.
The resume text contains visible candidate information.
Do not return an empty/default JSON object.
At minimum, extract visible name, email, phone, title/headline, summary, skills, experience, and education when present.
"""

        return f"""
You are an enterprise resume parser for a candidate-job recommendation system.

Return ONLY valid JSON.
Do not include markdown fences.
Do not include explanations.

Critical rules:
1. Extract only facts visible in the resume text.
2. Do not invent missing information.
3. Use null only when a field is genuinely missing.
4. Never return an empty/default JSON object when the resume text contains visible candidate information.
5. If an email is visible, contact.email must be populated.
6. If a phone number is visible, contact.phone must be populated.
7. If a name is visible at the top, contact.full_name must be populated.
8. Extract skills aggressively from summary, technical skills tables, and experience sections.
9. Extract work experience as a list of roles. Each role may include company, title, dates, responsibilities, and technologies.
10. Extract education and certifications when present.
11. Keep responsibilities concise. Use the strongest 3-6 bullets per role.
12. Use exactly the JSON keys shown in the output template.

Experience item format:
{{
  "company": null,
  "title": null,
  "location": null,
  "start_date": null,
  "end_date": null,
  "is_current": null,
  "responsibilities": [],
  "technologies": []
}}

Education item format:
{{
  "institution": null,
  "degree": null,
  "field_of_study": null,
  "start_date": null,
  "end_date": null,
  "score_or_grade": null
}}

Project item format:
{{
  "name": null,
  "description": null,
  "technologies": [],
  "url": null
}}

Required fixed values:
resume_id = {resume_id}
source_file_name = {file_name}
sha256 = {sha256}

Detected resume signals:
{json.dumps(signals, indent=2, ensure_ascii=False)}

{retry_instruction}

JSON output template:
{json.dumps(output_template, indent=2, ensure_ascii=False)}

Resume text:
<<<RESUME_TEXT_START
{resume_text}
RESUME_TEXT_END>>>
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
                "num_ctx": getattr(self.settings, "num_ctx", 8192),
            },
        }

        response = requests.post(
            url,
            json=payload,
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()

        data = response.json()
        return data.get("response") or ""

    def extract(
        self,
        markdown: str,
        *,
        resume_id: str,
        file_name: str,
        sha256: str,
        ocr_markdown_path: str | None,
    ) -> ResumeProfile:
        last_response_text = ""

        for strict_retry in (False, True):
            prompt = self._build_prompt(
                markdown,
                resume_id=resume_id,
                file_name=file_name,
                sha256=sha256,
                strict_retry=strict_retry,
            )

            response_text = self._call_ollama(prompt)
            last_response_text = response_text

            payload = _extract_json(response_text)

            if not isinstance(payload, dict):
                if strict_retry:
                    raise ValueError(
                        "Resume extraction did not return a JSON object. "
                        f"Raw response: {response_text[:1000]}"
                    )
                continue

            payload["resume_id"] = resume_id
            payload["source_file_name"] = file_name
            payload["sha256"] = sha256
            payload["raw_ocr_markdown_path"] = ocr_markdown_path

            try:
                profile = ResumeProfile.model_validate(payload)
            except ValidationError as exc:
                if strict_retry:
                    raise ValueError(f"Resume extraction JSON failed schema validation: {exc}") from exc
                continue

            if _profile_has_content(profile):
                return profile

        raise ValueError(
            "Resume extraction returned an empty/default profile even though OCR/native text was available. "
            f"Raw LLM response: {last_response_text[:1000]}"
        )