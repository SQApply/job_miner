from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..schemas import EducationItem, ExperienceItem, LlmSettings, ProjectItem, ResumeProfile


SECTION_HEADERS = {
    "PROFILE SUMMARY",
    "PROFESSIONAL SUMMARY",
    "SUMMARY",
    "CAREER SUMMARY",
    "OBJECTIVE",
    "TECHNICAL SKILLS",
    "KEY SKILLS",
    "SKILLS",
    "CORE SKILLS",
    "TECHNOLOGIES",
    "EDUCATION",
    "ACADEMIC DETAILS",
    "ACADEMICS",
    "PROJECTS UNDERTAKEN",
    "PROJECT HIGHLIGHT",
    "PROJECT HIGHLIGHTS",
    "PROJECTS",
    "WORK EXPERIENCE",
    "PROFESSIONAL EXPERIENCE",
    "EMPLOYMENT HISTORY",
    "WORK HISTORY",
    "EXPERIENCE",
    "CERTIFICATION",
    "CERTIFICATIONS",
    "AWARDS",
    "REWARDS",
    "ACHIEVEMENTS",
    "LANGUAGES",
}

CANONICAL_SECTION_TYPE_BY_HEADER = {
    "PROFILE SUMMARY": "summary",
    "PROFESSIONAL SUMMARY": "summary",
    "SUMMARY": "summary",
    "CAREER SUMMARY": "summary",
    "OBJECTIVE": "summary",
    "TECHNICAL SKILLS": "skills",
    "KEY SKILLS": "skills",
    "SKILLS": "skills",
    "CORE SKILLS": "skills",
    "TECHNOLOGIES": "skills",
    "WORK EXPERIENCE": "work_experience",
    "PROFESSIONAL EXPERIENCE": "work_experience",
    "EMPLOYMENT HISTORY": "work_experience",
    "WORK HISTORY": "work_experience",
    "EXPERIENCE": "work_experience",
    "PROJECTS UNDERTAKEN": "projects",
    "PROJECT HIGHLIGHT": "projects",
    "PROJECT HIGHLIGHTS": "projects",
    "PROJECTS": "projects",
    "EDUCATION": "education",
    "ACADEMIC DETAILS": "education",
    "ACADEMICS": "education",
    "CERTIFICATION": "certifications",
    "CERTIFICATIONS": "certifications",
    "AWARDS": "achievements",
    "REWARDS": "achievements",
    "ACHIEVEMENTS": "achievements",
    "LANGUAGES": "languages",
}

DATE_RANGE_PATTERN = re.compile(
    r"(?P<start>(?:[A-Za-z]{3,9}\s+)?\d{4}|\d{1,2}/\d{4}|\d{1,2}[-/]\d{4}|\d{4})"
    r"\s*(?:-|–|—|to|until|through)\s*"
    r"(?P<end>present|current|currently\s+working|till\s+date|now|(?:[A-Za-z]{3,9}\s+)?\d{4}|\d{1,2}/\d{4}|\d{1,2}[-/]\d{4}|\d{4})",
    flags=re.IGNORECASE,
)

BULLET_PREFIX_PATTERN = re.compile(r"^\s*(?:[-*•▪▫●◦‣❖]+|\d+[.)])\s*")


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
    """Keep useful parts of long resumes: beginning plus ending."""
    markdown = markdown.strip()

    if len(markdown) <= max_chars:
        return markdown

    marker = "\n\n[...RESUME_TEXT_TRUNCATED_FOR_EXTRACTION...]\n\n"
    head_chars = int(max_chars * 0.70)
    tail_chars = max_chars - head_chars - len(marker)

    if tail_chars <= 0:
        return markdown[:max_chars]

    return markdown[:head_chars] + marker + markdown[-tail_chars:]


def _clean_header(value: str) -> str:
    value = re.sub(r"^[^\w]+", "", value or "").strip()
    value = re.sub(r"[:\-]+$", "", value).strip()
    return re.sub(r"\s+", " ", value).upper()


def _looks_like_section_header(line: str) -> bool:
    return _clean_header(line) in SECTION_HEADERS


def _extract_section(markdown: str, header: str, *, max_chars: int = 4000) -> str:
    """Extract a resume section for prompt guidance only.

    This does not repair or fill profile fields. It only gives the LLM clearer
    context so WORK EXPERIENCE and PROJECTS are not mixed.
    """
    target = _clean_header(header)
    lines = markdown.splitlines()
    collected: list[str] = []
    in_section = False

    for line in lines:
        clean = line.strip()
        if not clean:
            if in_section:
                collected.append("")
            continue

        if _clean_header(clean) == target:
            in_section = True
            continue

        if in_section and _looks_like_section_header(clean):
            break

        if in_section:
            collected.append(clean)

    text = "\n".join(collected).strip()
    return text[:max_chars]



def _section_hints_for_prompt(markdown: str) -> dict[str, str]:
    return {
        "profile_summary_section": _extract_section(markdown, "PROFILE SUMMARY", max_chars=2500)
        or _extract_section(markdown, "PROFESSIONAL SUMMARY", max_chars=2500)
        or _extract_section(markdown, "SUMMARY", max_chars=2500),
        "technical_skills_section": _extract_section(markdown, "TECHNICAL SKILLS", max_chars=3500)
        or _extract_section(markdown, "KEY SKILLS", max_chars=3500)
        or _extract_section(markdown, "SKILLS", max_chars=3500),
        "education_section": _extract_section(markdown, "EDUCATION", max_chars=2000)
        or _extract_section(markdown, "ACADEMIC DETAILS", max_chars=2000),
        "projects_section": _extract_section(markdown, "PROJECTS UNDERTAKEN", max_chars=6000)
        or _extract_section(markdown, "PROJECT HIGHLIGHT", max_chars=6000)
        or _extract_section(markdown, "PROJECT HIGHLIGHTS", max_chars=6000)
        or _extract_section(markdown, "PROJECTS", max_chars=6000),
        "work_experience_section": _extract_section(markdown, "WORK EXPERIENCE", max_chars=5000)
        or _extract_section(markdown, "PROFESSIONAL EXPERIENCE", max_chars=5000)
        or _extract_section(markdown, "EMPLOYMENT HISTORY", max_chars=5000)
        or _extract_section(markdown, "EXPERIENCE", max_chars=5000),
    }


def _line_without_bullet(value: str) -> str:
    return BULLET_PREFIX_PATTERN.sub("", value or "").strip()


def _clean_resume_lines(markdown: str) -> list[dict[str, Any]]:
    """Create stable line IDs from OCR/native markdown.

    This is Layer 1.5: it preserves line order and basic bullet information
    before Layer 2 section/block normalization.
    """
    lines: list[dict[str, Any]] = []

    for raw_idx, raw_line in enumerate(markdown.splitlines(), start=1):
        text = re.sub(r"\s+", " ", raw_line or "").strip()
        if not text:
            continue

        # Ignore common synthetic page markers while preserving real resume text.
        if re.match(r"^<?PARSED TEXT FOR PAGE\b", text, flags=re.IGNORECASE):
            continue
        if re.match(r"^<?IMAGE FOR PAGE\b", text, flags=re.IGNORECASE):
            continue

        lines.append(
            {
                "line_id": f"L{len(lines) + 1:04d}",
                "raw_index": raw_idx,
                "text": text,
                "text_without_bullet": _line_without_bullet(text),
                "is_bullet": bool(BULLET_PREFIX_PATTERN.match(text)),
                "date_ranges": [match.group(0).strip() for match in DATE_RANGE_PATTERN.finditer(text)],
            }
        )

    return lines


def _canonical_section_type(header: str) -> str:
    return CANONICAL_SECTION_TYPE_BY_HEADER.get(_clean_header(header), "other")


def _segment_resume_sections(markdown: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split lines into canonical resume sections.

    This is not extraction repair. It only converts messy resume text into a
    predictable document representation for the LLM.
    """
    lines = _clean_resume_lines(markdown)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] = {
        "section_id": "sec_000_header",
        "canonical_type": "header",
        "original_heading": "HEADER",
        "lines": [],
    }

    for line in lines:
        clean = _clean_header(line["text"])
        if clean in SECTION_HEADERS:
            if current["lines"] or current["canonical_type"] != "header":
                sections.append(current)
            current = {
                "section_id": f"sec_{len(sections) + 1:03d}_{_canonical_section_type(clean)}",
                "canonical_type": _canonical_section_type(clean),
                "original_heading": line["text"],
                "lines": [],
            }
            continue

        current["lines"].append(line)

    if current["lines"] or current["canonical_type"] != "header":
        sections.append(current)

    return lines, sections


def _compact_line(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_id": line["line_id"],
        "text": line["text_without_bullet"] if line.get("is_bullet") else line["text"],
        "is_bullet": line.get("is_bullet", False),
        "date_ranges": line.get("date_ranges", []),
    }


def _looks_like_role_start(line: dict[str, Any], *, allow_bullets: bool = False) -> bool:
    if line.get("is_bullet") and not allow_bullets:
        return False

    text = line.get("text_without_bullet") or line.get("text", "")
    if DATE_RANGE_PATTERN.search(text):
        return True

    # Some resumes split title/company/date into adjacent lines. Keep this
    # broad; the LLM receives the final evidence block, not final field values.
    return bool(
        re.search(
            r"\b(engineer|developer|scientist|analyst|manager|consultant|architect|lead|intern|associate|officer|specialist|administrator|programmer)\b",
            text,
            flags=re.IGNORECASE,
        )
        and len(text.split()) <= 12
    )


def _build_work_role_blocks(section: dict[str, Any], *, max_blocks: int = 20, max_bullets_per_block: int = 18) -> list[dict[str, Any]]:
    """Group WORK EXPERIENCE into role evidence blocks.

    The previous implementation treated bullet lines as non-role starts. That
    fails for resumes where each job row is a bullet, for example:
    "Assistant Vice President at Company (2025-current)".

    This function still does not extract final fields. It only creates better
    evidence blocks for the LLM.
    """
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def new_block() -> dict[str, Any]:
        return {
            "block_id": f"{section['section_id']}_role_{len(blocks) + 1:03d}",
            "source_section": section.get("original_heading"),
            "canonical_section": section.get("canonical_type"),
            "lines": [],
            "context_lines": [],
            "bullets": [],
            "date_ranges": [],
            "has_date_evidence": False,
            "field_hints": {
                "title_company_dates_may_be_same_line": False,
                "company_location_may_be_adjacent_line": False,
                "bullet_line_can_be_role_start": False,
            },
        }

    # def flush_current() -> None:
    #     nonlocal current
    #     if current is not None and current.get("lines"):
    #         current["evidence_text"] = "\n".join(line["text"] for line in current["lines"][:30])
    #         blocks.append(current)
    #     current = None
    def flush_current() -> None:
        nonlocal current
        if current is not None:
            current["evidence_text"] = "\n".join(line["text"] for line in current["lines"][:30])
            inferred_hints = _infer_role_field_hints(current)
            current.setdefault("field_hints", {}).update(inferred_hints)
            blocks.append(current)
            current = None

    for line in section.get("lines", []):
        text = line.get("text_without_bullet") or line.get("text", "")
        if not text:
            continue

        # In WORK EXPERIENCE, bullets can be either responsibilities or compact
        # role rows. If a bullet contains a date range or looks like a short role
        # line, treat it as a role start.
        starts_role = _looks_like_role_start(line, allow_bullets=True)
        has_dates = bool(line.get("date_ranges")) or bool(DATE_RANGE_PATTERN.search(text))

        if starts_role and current is not None and current.get("lines"):
            # A new title/date line starts a new role block. This is what fixes
            # one-block IR for bullet-style work history.
            flush_current()

        if current is None:
            current = new_block()

        compact = _compact_line(line)
        current["lines"].append(compact)

        if has_dates:
            current["has_date_evidence"] = True
            for date_range in line.get("date_ranges", []):
                if date_range not in current["date_ranges"]:
                    current["date_ranges"].append(date_range)

        lowered = f" {text.lower()} "
        if has_dates and " at " in lowered:
            current["field_hints"]["title_company_dates_may_be_same_line"] = True
        elif has_dates:
            current["field_hints"]["company_location_may_be_adjacent_line"] = True

        if line.get("is_bullet") and starts_role:
            current["field_hints"]["bullet_line_can_be_role_start"] = True
        elif line.get("is_bullet"):
            if len(current["bullets"]) < max_bullets_per_block:
                current["bullets"].append(compact)
        else:
            current["context_lines"].append(compact)

    flush_current()
    return blocks[:max_blocks]



def _normalize_date_value(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    lowered = text.lower()
    if any(token in lowered for token in ("present", "current", "currently working", "now")):
        return "present"

    return text

def _retry_payload_for_prompt(
    previous_payload: dict[str, Any] | None,
    quality_errors: list[str] | None,
) -> dict[str, Any]:
    payload = deepcopy(previous_payload or {})
    errors_text = " ".join(quality_errors or [])

    if "WORK EXPERIENCE" in errors_text:
        # Previous experience is known bad. Do not let the retry copy it.
        payload["experience"] = []
        payload["current_title"] = None
        payload["current_company"] = None

    if "domains[] is empty" in errors_text:
        payload["domains"] = []

    return payload


def _split_date_range(value: str | None) -> tuple[str | None, str | None, bool | None]:
    text = str(value or "").strip()
    if not text:
        return None, None, None

    normalized = (
        text.replace("–", "-")
        .replace("—", "-")
        .replace("to", "-", 1)
        .strip()
    )

    parts = [part.strip(" .()") for part in normalized.split("-", 1)]
    if len(parts) == 1:
        start = _normalize_date_value(parts[0])
        return start, None, None

    start = _normalize_date_value(parts[0])
    end = _normalize_date_value(parts[1])
    is_current = end == "present"

    return start, end, is_current


def _infer_role_field_hints(block: dict[str, Any]) -> dict[str, Any]:
    """Infer role-block hints before the LLM.

    This is not final profile extraction and does not write to Mongo.
    These hints only make canonical_resume_json easier for the LLM to consume.
    """
    evidence_text = str(block.get("evidence_text") or "").strip()
    lines = [str(line.get("text") or "").strip() for line in block.get("lines", [])]
    context_lines = [str(line.get("text") or "").strip() for line in block.get("context_lines", [])]

    hints: dict[str, Any] = {}

    # Pattern: Title at Company (date-range)
    match = re.search(
        r"^(?P<title>.+?)\s+at\s+(?P<company>.+?)\s*\((?P<dates>[^)]+)\)\.?\s*$",
        evidence_text,
        flags=re.IGNORECASE,
    )

    if match:
        start_date, end_date, is_current = _split_date_range(match.group("dates"))
        hints.update(
            {
                "possible_title": match.group("title").strip(),
                "possible_company": match.group("company").strip(),
                "possible_start_date": start_date,
                "possible_end_date": end_date,
                "possible_is_current": is_current,
                "hint_source": "title_at_company_date_pattern",
            }
        )
        return {key: value for key, value in hints.items() if value is not None}

    # Pattern: Title date-range, company/location nearby
    for idx, line in enumerate(lines):
        date_match = DATE_RANGE_PATTERN.search(line)
        if not date_match:
            continue

        title = line[: date_match.start()].strip(" -|,")
        start_date, end_date, is_current = _split_date_range(date_match.group(0))

        if title:
            hints["possible_title"] = title

        hints["possible_start_date"] = start_date
        hints["possible_end_date"] = end_date
        hints["possible_is_current"] = is_current

        nearby_lines = lines[idx + 1 :] + context_lines
        if nearby_lines:
            company_location = nearby_lines[0].strip()
            hints["possible_company_location_line"] = company_location
            # Let the LLM decide exact company/location split for this layout.
            # Example: "Ericsson Noida, India"
            hints["possible_company"] = company_location

        hints["hint_source"] = "title_date_company_adjacent_pattern"
        return {key: value for key, value in hints.items() if value is not None}

    return hints

def _build_skill_groups(section: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    uncategorized: list[str] = []

    for line in section.get("lines", []):
        text = line.get("text_without_bullet") or line.get("text") or ""
        if not text:
            continue

        if ":" in text:
            category, values = text.split(":", 1)
            groups.append(
                {
                    "block_id": f"{section['section_id']}_skill_{len(groups) + 1:03d}",
                    "category": category.strip(),
                    "values_text": values.strip(),
                    "evidence_text": text,
                }
            )
        else:
            uncategorized.append(text)

    if uncategorized:
        groups.append(
            {
                "block_id": f"{section['section_id']}_skill_{len(groups) + 1:03d}",
                "category": "uncategorized",
                "values_text": ", ".join(uncategorized),
                "evidence_text": "\n".join(uncategorized[:20]),
            }
        )

    return groups


def _build_simple_blocks(section: dict[str, Any], *, prefix: str, max_blocks: int = 20) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current_lines: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current_lines
        if current_lines:
            blocks.append(
                {
                    "block_id": f"{section['section_id']}_{prefix}_{len(blocks) + 1:03d}",
                    "source_section": section.get("original_heading"),
                    "lines": [_compact_line(line) for line in current_lines[:25]],
                    "evidence_text": "\n".join(line["text"] for line in current_lines[:25]),
                }
            )
            current_lines = []

    for line in section.get("lines", []):
        starts_new = line.get("is_bullet") or bool(line.get("date_ranges"))
        if starts_new and current_lines:
            flush()
        current_lines.append(line)

    flush()
    return blocks[:max_blocks]


def _build_canonical_resume_ir(markdown: str) -> dict[str, Any]:
    """Layer 2: canonical resume structure normalized as JSON.

    The LLM uses this as the primary source. The raw resume text remains in the
    prompt as a fallback, but extraction should come from these blocks.
    """
    lines, sections = _segment_resume_sections(markdown)
    canonical_sections: list[dict[str, Any]] = []

    for section in sections:
        canonical_type = section.get("canonical_type", "other")
        item: dict[str, Any] = {
            "section_id": section["section_id"],
            "canonical_type": canonical_type,
            "original_heading": section.get("original_heading"),
            "line_count": len(section.get("lines", [])),
            "lines": [_compact_line(line) for line in section.get("lines", [])[:80]],
        }

        if canonical_type == "work_experience":
            item["role_blocks"] = _build_work_role_blocks(section)
        elif canonical_type == "skills":
            item["skill_groups"] = _build_skill_groups(section)
        elif canonical_type == "projects":
            item["project_blocks"] = _build_simple_blocks(section, prefix="project")
        elif canonical_type == "education":
            item["education_blocks"] = _build_simple_blocks(section, prefix="education")
        elif canonical_type in {"certifications", "achievements", "languages"}:
            item["blocks"] = _build_simple_blocks(section, prefix=canonical_type)

        canonical_sections.append(item)

    return {
        "document_stats": {
            "line_count": len(lines),
            "section_count": len(canonical_sections),
        },
        "header_lines": [
            _compact_line(line)
            for section in sections
            if section.get("canonical_type") == "header"
            for line in section.get("lines", [])[:20]
        ],
        "sections": canonical_sections,
        "instructions_for_llm": {
            "primary_source": "Use sections[].role_blocks, skill_groups, project_blocks, and education_blocks as structured evidence.",
            "evidence_rule": "Every extracted item should cite source_section, block_id, and evidence_text from this JSON when possible.",
            "no_repair_rule": "This JSON is evidence organization only; final field extraction must still be done by the LLM.",
        },
    }


def _write_debug_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".txt", ".md"}:
        path.write_text(str(payload), encoding="utf-8")
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _dump_temp_extracted_artifact(
    *,
    resume_id: str,
    stage: str,
    payload: Any,
    extension: str = "json",
) -> str | None:
    """Write every important intermediate artifact to temp_extracted_files.

    This folder is intended for local debugging only and may contain PII.
    """
    enabled = os.getenv("JOB_MINER_TEMP_EXTRACT_DEBUG", "true").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None

    root = Path(os.getenv("JOB_MINER_TEMP_EXTRACT_DIR", "temp_extracted_files"))
    safe_stage = re.sub(r"[^a-zA-Z0-9_.-]+", "_", stage).strip("_")
    extension = extension.lstrip(".") or "json"
    path = root / resume_id / f"{safe_stage}.{extension}"
    _write_debug_file(path, payload)
    return str(path)


def _dump_llm_artifact(
    *,
    resume_id: str,
    stage: str,
    payload: dict[str, Any],
) -> str | None:
    """Write raw LLM prompts/responses/payloads for debugging.

    These files can contain candidate PII, so they are written under local
    debug directories that must stay git-ignored.
    """
    enabled = os.getenv("JOB_MINER_RESUME_LLM_DEBUG", "true").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_stage = re.sub(r"[^a-zA-Z0-9_.-]+", "_", stage).strip("_")

    debug_root = Path(os.getenv("JOB_MINER_RESUME_LLM_DEBUG_DIR", "data/resumes/llm_debug"))
    debug_path = debug_root / resume_id / f"{timestamp}_{safe_stage}.json"
    _write_debug_file(debug_path, payload)

    # Mirror every LLM artifact into temp_extracted_files with stable stage names
    # so repeated test runs are easy to inspect without opening the timestamped folder.
    _dump_temp_extracted_artifact(
        resume_id=resume_id,
        stage=f"llm_{safe_stage}",
        payload={"timestamp": timestamp, **payload},
        extension="json",
    )

    return str(debug_path)


def _basic_resume_signals(markdown: str) -> dict[str, Any]:
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


def _experience_item_has_core_fields(item: ExperienceItem) -> bool:
    return bool(item.company and item.title and (item.start_date or item.end_date or item.is_current))


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


def _profile_quality_errors(profile: ResumeProfile, markdown: str) -> list[str]:
    """Validate extraction quality without repairing the output."""
    errors: list[str] = []
    sections = _section_hints_for_prompt(markdown)

    work_experience_section = sections.get("work_experience_section", "")
    projects_section = sections.get("projects_section", "")
    technical_skills_section = sections.get("technical_skills_section", "")
    education_section = sections.get("education_section", "")

    if work_experience_section:
        strong_roles = [item for item in profile.experience if _experience_item_has_core_fields(item)]

        if not strong_roles:
            errors.append(
                "WORK EXPERIENCE exists, but experience[] has no role with company + title + date/current fields."
            )

        if not profile.current_title:
            errors.append("WORK EXPERIENCE exists, but current_title is null.")

        if not profile.current_company:
            errors.append("WORK EXPERIENCE exists, but current_company is null.")

        project_like_experience = [
            item
            for item in profile.experience
            if not item.company and not item.title and (item.responsibilities or item.technologies)
        ]
        if project_like_experience:
            errors.append(
                "experience[] contains project-like bullets without company/title. Projects must be in projects[], not experience[]."
            )

    if projects_section and not any(_project_item_has_content(item) for item in profile.projects):
        errors.append("PROJECTS section exists, but projects[] is empty.")

    if education_section and not any(_education_item_has_content(item) for item in profile.education):
        errors.append("EDUCATION section exists, but education[] is empty or contains only null fields.")

    if technical_skills_section:
        has_skills = any(
            [
                profile.primary_skills,
                profile.secondary_skills,
                profile.tools_and_platforms,
                profile.programming_languages,
            ]
        )
        if not has_skills:
            errors.append("TECHNICAL SKILLS section exists, but no skills were extracted.")

    if re.search(r"\b(?:more than\s+)?\d{1,2}\+?\s+years\b", markdown, flags=re.IGNORECASE):
        if profile.total_experience_years is None:
            errors.append("Resume states total years of experience, but total_experience_years is null.")

    if not profile.domains:
        errors.append("domains[] is empty. Infer domains from summary, skills, projects, and work experience.")

    return errors

def _critical_quality_errors(errors: list[str]) -> list[str]:
    critical_markers = (
        "WORK EXPERIENCE exists, but experience[] has no role",
        "WORK EXPERIENCE exists, but current_title is null",
        "WORK EXPERIENCE exists, but current_company is null",
        "experience[] contains project-like bullets",
        "Resume states total years of experience, but total_experience_years is null",
    )

    return [
        error
        for error in errors
        if any(marker in error for marker in critical_markers)
    ]

def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True

    if isinstance(value, str):
        return not value.strip()

    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0

    return False


def _dedupe_string_list(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)

    return result


def _item_identity(item: Any) -> str:
    if not isinstance(item, dict):
        return repr(item)

    keys = [
        item.get("name"),
        item.get("title"),
        item.get("company"),
        item.get("institution"),
        item.get("degree"),
        item.get("start_date"),
        item.get("end_date"),
    ]
    identity = "|".join(str(value or "").strip().casefold() for value in keys)
    return identity or repr(item)


def _merge_list_preserving_previous(first: list[Any], retry: list[Any]) -> list[Any]:
    if not retry:
        return deepcopy(first)

    if not first:
        return deepcopy(retry)

    if all(not isinstance(item, dict) for item in first + retry):
        return _dedupe_string_list([*first, *retry])

    result = deepcopy(retry)
    seen = {_item_identity(item) for item in result}

    for item in first:
        key = _item_identity(item)
        if key not in seen:
            result.append(deepcopy(item))
            seen.add(key)

    return result


def _dict_experience_has_core_fields(item: Any) -> bool:
    if not isinstance(item, dict):
        return False

    return bool(
        item.get("company")
        and item.get("title")
        and (item.get("start_date") or item.get("end_date") or item.get("is_current"))
    )


def _merge_retry_payload(
    first_payload: dict[str, Any],
    retry_payload: dict[str, Any],
    *,
    resume_id: str,
    file_name: str,
    sha256: str,
    ocr_markdown_path: str | None,
    first_quality_errors: list[str],
    retry_quality_errors: list[str],
    debug_paths: list[str],
) -> dict[str, Any]:
    """Merge retry output without losing useful first-attempt values.

    This is not resume-text post-processing. It only protects good first LLM
    values from being dropped by the correction retry.
    """
    merged = deepcopy(first_payload)

    for key, retry_value in retry_payload.items():
        if key in {"resume_id", "source_file_name", "sha256", "raw_ocr_markdown_path"}:
            continue

        first_value = merged.get(key)

        if _is_empty_value(retry_value):
            continue

        if isinstance(first_value, dict) and isinstance(retry_value, dict):
            next_value = deepcopy(first_value)
            for nested_key, nested_retry_value in retry_value.items():
                if _is_empty_value(nested_retry_value):
                    continue
                next_value[nested_key] = nested_retry_value
            merged[key] = next_value
            continue

        if isinstance(first_value, list) and isinstance(retry_value, list):
            if key == "experience":
                # Allow retry to correct bad project-as-experience output.
                # If retry has real role rows, use it. Otherwise preserve first.
                if any(_dict_experience_has_core_fields(item) for item in retry_value):
                    merged[key] = retry_value
                else:
                    merged[key] = _merge_list_preserving_previous(first_value, retry_value)
            else:
                merged[key] = _merge_list_preserving_previous(first_value, retry_value)
            continue

        merged[key] = retry_value

    merged["resume_id"] = resume_id
    merged["source_file_name"] = file_name
    merged["sha256"] = sha256
    merged["raw_ocr_markdown_path"] = ocr_markdown_path

    parse_warnings = _dedupe_string_list(
        [
            *merged.get("parse_warnings", []),
            *[f"first_attempt_quality_gate: {error}" for error in first_quality_errors],
            *[f"retry_quality_gate_remaining: {error}" for error in retry_quality_errors],
        ]
    )

    merged["parse_warnings"] = parse_warnings
    merged["extraction_quality"] = {
        "first_attempt_quality_errors": first_quality_errors,
        "retry_quality_errors": retry_quality_errors,
        "accepted_after_retry": True,
        "llm_debug_paths": debug_paths,
        "merge_policy": (
            "Retry may correct failed fields. Non-empty contact, summary, skills, education, "
            "projects, and other non-conflicting first-attempt values are preserved."
        ),
    }

    return merged


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
        is_correction_retry: bool = False,
        previous_payload: dict[str, Any] | None = None,
        previous_quality_errors: list[str] | None = None,
    ) -> str:
        # Primary input to the LLM: structured canonical resume evidence.
        # Raw text is included only as a small backup, not as the main source.
        canonical_resume_json = _build_canonical_resume_ir(markdown)
        canonical_resume_json_text = json.dumps(
            canonical_resume_json,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw_resume_backup = _resume_text_window(markdown, 4000)
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
            "extraction_quality": {},
        }

        experience_item_format = {
            "company": None,
            "title": None,
            "location": None,
            "start_date": None,
            "end_date": None,
            "is_current": None,
            "responsibilities": [],
            "technologies": [],
            "evidence": {
                "source_section": None,
                "block_id": None,
                "evidence_text": None,
            },
        }

        education_item_format = {
            "institution": None,
            "degree": None,
            "field_of_study": None,
            "start_date": None,
            "end_date": None,
            "score_or_grade": None,
            "evidence": {
                "source_section": None,
                "block_id": None,
                "evidence_text": None,
            },
        }

        project_item_format = {
            "name": None,
            "description": None,
            "technologies": [],
            "url": None,
            "evidence": {
                "source_section": None,
                "block_id": None,
                "evidence_text": None,
            },
        }

        retry_instruction = ""
        if is_correction_retry:
            safe_previous_payload = _retry_payload_for_prompt(previous_payload, previous_quality_errors)
            retry_instruction = f"""
CORRECTION RETRY MODE:
The previous JSON was schema-valid but failed quality checks.

Quality errors:
{json.dumps(previous_quality_errors or [], indent=2, ensure_ascii=False)}

Previous extracted JSON with known-bad fields removed:
{json.dumps(safe_previous_payload, indent=2, ensure_ascii=False)}

Retry instructions:
1. Preserve correct non-null values from the previous JSON.
2. If WORK EXPERIENCE failed, rebuild experience[] from canonical_resume_json.sections[].role_blocks.
3. Do not copy previous broken experience[] rows.
4. Use role_block.field_hints first when available.
5. If role_block.field_hints.possible_company exists, experience[].company must equal that value.
6. If role_block.field_hints.possible_title exists, experience[].title must equal that value.
7. If role_block.field_hints.possible_start_date exists, experience[].start_date must equal that value.
8. If role_block.field_hints.possible_end_date exists, experience[].end_date must equal that value.
9. If role_block.field_hints.possible_is_current exists, experience[].is_current must equal that value.
10. Fill current_title/current_company from the current role, or from the most recent role when no current role exists.
11. Fill total_experience_years from explicit summary evidence when visible.
12. Return the complete corrected JSON object, not a patch/diff.
"""

        return f"""
You are an enterprise-grade resume parser.

Return ONLY valid JSON.
No markdown.
No explanations.
Use exactly the JSON keys in the output template.

PRIMARY SOURCE:
Use canonical_resume_json as the primary evidence source.
Use raw_resume_text_backup only if canonical_resume_json is missing a visible detail.

Extraction rules:
1. Extract only facts visible in canonical_resume_json or raw_resume_text_backup.
2. Do not invent data.
3. Use null only when genuinely missing.
4. Dates must be strings, never numbers.
5. Do not convert year-only dates into full dates. Use "2023", not "2023-01-01".
6. Every experience, education, and project item should include evidence.source_section, evidence.block_id when available, and evidence.evidence_text.

Section rules:
7. Use work_experience role_blocks only for experience[].
8. Use project/project_highlight blocks only for projects[].
9. Use skill_groups only for skill fields.
10. Use education_blocks only for education[].
11. Never mix projects into experience[].

Experience extraction contract:
12. Extract one experience item per role_block.
13. For each role_block, use field_hints first.
14. If role_block.field_hints.possible_company exists, experience[].company must equal that value.
15. If role_block.field_hints.possible_title exists, experience[].title must equal that value.
16. If role_block.field_hints.possible_start_date exists, experience[].start_date must equal that value.
17. If role_block.field_hints.possible_end_date exists, experience[].end_date must equal that value.
18. If role_block.field_hints.possible_is_current exists, experience[].is_current must equal that value.
19. If field_hints are missing, infer title, company, location, and dates from nearby lines inside the same role_block.
20. Never return company=null when a company is visible in the same role_block.
21. Never return title=null when a role/designation is visible in the same role_block.
22. responsibilities should come from bullets inside the same role_block.
23. evidence.source_section must use the original section heading.
24. evidence.block_id must use the role block_id from canonical_resume_json.
25. evidence.evidence_text must be the exact role-block lines or a close excerpt from the role block.

Root profile rules:
26. current_title and current_company must come from the current role if present.
27. If no current role exists, use the most recent role.
28. total_experience_years must come from explicit summary evidence such as "over 3.8 years", "more than 13 years", or "5+ years".
29. domains[] must be inferred from summary, skills, projects, and experience evidence.

Quality requirements:
30. If work_experience role_blocks exist, experience[] must contain role items with company + title + date/current status.
31. If skill_groups exist, skill fields must be populated.
32. If education_blocks exist, education[] must be populated.
33. If project_blocks exist, projects[] must be populated.

Output size limits:
34. Keep summary concise, maximum 700 characters.
35. primary_skills: maximum 35 items.
36. secondary_skills: maximum 25 items.
37. tools_and_platforms: maximum 35 items.
38. programming_languages: maximum 15 items.
39. domains: maximum 10 items.
40. experience[].responsibilities: maximum 6 strongest bullets per role.
41. projects: maximum 8 strongest projects.
42. projects[].description: maximum 250 characters.
43. Remove duplicates across skill lists where possible.
44. Do not include long prose when a short factual value is enough.

Fixed values:
resume_id = {resume_id}
source_file_name = {file_name}
sha256 = {sha256}

Detected signals:
{json.dumps(signals, ensure_ascii=False, separators=(",", ":"))}

canonical_resume_json:
{canonical_resume_json_text}

{retry_instruction}

Experience item format:
{json.dumps(experience_item_format, ensure_ascii=False, separators=(",", ":"))}

Education item format:
{json.dumps(education_item_format, ensure_ascii=False, separators=(",", ":"))}

Project item format:
{json.dumps(project_item_format, ensure_ascii=False, separators=(",", ":"))}

JSON output template:
{json.dumps(output_template, ensure_ascii=False, separators=(",", ":"))}

raw_resume_text_backup:
<<<RAW_TEXT_BACKUP_START
{raw_resume_backup}
RAW_TEXT_BACKUP_END>>>
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

    def _validate_payload(
        self,
        payload: dict[str, Any],
        *,
        resume_id: str,
        file_name: str,
        sha256: str,
        ocr_markdown_path: str | None,
    ) -> ResumeProfile:
        payload["resume_id"] = resume_id
        payload["source_file_name"] = file_name
        payload["sha256"] = sha256
        payload["raw_ocr_markdown_path"] = ocr_markdown_path

        return ResumeProfile.model_validate(payload)

    def extract(
        self,
        markdown: str,
        *,
        resume_id: str,
        file_name: str,
        sha256: str,
        ocr_markdown_path: str | None,
    ) -> ResumeProfile:
        debug_paths: list[str] = []

        _dump_temp_extracted_artifact(
            resume_id=resume_id,
            stage="00_input_markdown",
            payload=markdown,
            extension="md",
        )
        clean_lines, segmented_sections = _segment_resume_sections(markdown)
        _dump_temp_extracted_artifact(
            resume_id=resume_id,
            stage="01_clean_lines",
            payload={"lines": clean_lines},
        )
        _dump_temp_extracted_artifact(
            resume_id=resume_id,
            stage="02_segmented_sections",
            payload={"sections": segmented_sections},
        )

        canonical_resume = _build_canonical_resume_ir(markdown)
        canonical_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="03_canonical_resume_ir",
            payload={"canonical_resume": canonical_resume},
        )
        if canonical_debug_path:
            debug_paths.append(canonical_debug_path)

        first_prompt = self._build_prompt(
            markdown,
            resume_id=resume_id,
            file_name=file_name,
            sha256=sha256,
            is_correction_retry=False,
        )
        first_prompt_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="01_first_prompt",
            payload={"prompt": first_prompt},
        )
        if first_prompt_debug_path:
            debug_paths.append(first_prompt_debug_path)

        first_response_text = self._call_ollama(first_prompt)
        first_response_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="02_first_response",
            payload={"response_text": first_response_text},
        )
        if first_response_debug_path:
            debug_paths.append(first_response_debug_path)

        first_payload = _extract_json(first_response_text)
        first_payload_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="03_first_payload",
            payload={"payload": first_payload},
        )
        if first_payload_debug_path:
            debug_paths.append(first_payload_debug_path)

        if not isinstance(first_payload, dict):
            _dump_llm_artifact(
                resume_id=resume_id,
                stage="07_first_failed_non_json",
                payload={"response_text": first_response_text, "debug_paths": debug_paths},
            )
            raise ValueError(
                "Resume extraction did not return a JSON object. "
                f"Raw response: {first_response_text[:1000]}"
            )

        try:
            first_profile = self._validate_payload(
                first_payload,
                resume_id=resume_id,
                file_name=file_name,
                sha256=sha256,
                ocr_markdown_path=ocr_markdown_path,
            )
        except ValidationError as exc:
            _dump_llm_artifact(
                resume_id=resume_id,
                stage="07_first_failed_schema_validation",
                payload={
                    "payload": first_payload,
                    "validation_error": str(exc),
                    "debug_paths": debug_paths,
                },
            )
            raise ValueError(f"Resume extraction JSON failed schema validation: {exc}") from exc

        if not _profile_has_content(first_profile):
            first_quality_errors = ["The extracted profile is empty/default even though OCR/native text is available."]
        else:
            first_quality_errors = _profile_quality_errors(first_profile, markdown)

        if not first_quality_errors:
            first_payload["extraction_quality"] = {
                "first_attempt_quality_errors": [],
                "accepted_after_retry": False,
                "llm_debug_paths": debug_paths,
            }
            final_debug_path = _dump_llm_artifact(
                resume_id=resume_id,
                stage="08_final_first_payload",
                payload={"payload": first_payload},
            )
            if final_debug_path:
                debug_paths.append(final_debug_path)
                first_payload["extraction_quality"]["llm_debug_paths"] = debug_paths

            return self._validate_payload(
                first_payload,
                resume_id=resume_id,
                file_name=file_name,
                sha256=sha256,
                ocr_markdown_path=ocr_markdown_path,
            )

        _dump_llm_artifact(
            resume_id=resume_id,
            stage="04_first_quality_errors",
            payload={
                "first_quality_errors": first_quality_errors,
                "first_payload": first_profile.model_dump(mode="json"),
                "debug_paths": debug_paths,
            },
        )

        retry_prompt = self._build_prompt(
            markdown,
            resume_id=resume_id,
            file_name=file_name,
            sha256=sha256,
            is_correction_retry=True,
            previous_payload=first_profile.model_dump(mode="json"),
            previous_quality_errors=first_quality_errors,
        )
        retry_prompt_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="05_retry_prompt",
            payload={
                "quality_errors": first_quality_errors,
                "previous_payload": first_profile.model_dump(mode="json"),
                "prompt": retry_prompt,
            },
        )
        if retry_prompt_debug_path:
            debug_paths.append(retry_prompt_debug_path)

        retry_response_text = self._call_ollama(retry_prompt)
        retry_response_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="06_retry_response",
            payload={"response_text": retry_response_text},
        )
        if retry_response_debug_path:
            debug_paths.append(retry_response_debug_path)

        retry_payload = _extract_json(retry_response_text)
        retry_payload_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="07_retry_payload",
            payload={"payload": retry_payload},
        )
        if retry_payload_debug_path:
            debug_paths.append(retry_payload_debug_path)

        if not isinstance(retry_payload, dict):
            _dump_llm_artifact(
                resume_id=resume_id,
                stage="07_retry_failed_non_json",
                payload={
                    "first_quality_errors": first_quality_errors,
                    "retry_response_text": retry_response_text,
                    "debug_paths": debug_paths,
                },
            )
            raise ValueError(
                "Resume extraction failed quality gate and correction retry did not return valid JSON. "
                + "; ".join(first_quality_errors)
            )

        try:
            retry_profile = self._validate_payload(
                retry_payload,
                resume_id=resume_id,
                file_name=file_name,
                sha256=sha256,
                ocr_markdown_path=ocr_markdown_path,
            )
            retry_quality_errors = _profile_quality_errors(retry_profile, markdown)
            remaining_critical_errors = _critical_quality_errors(retry_quality_errors)

            if remaining_critical_errors:
                _dump_llm_artifact(
                    resume_id=resume_id,
                    stage="08_retry_failed_critical_quality_gate",
                    payload={
                        "first_quality_errors": first_quality_errors,
                        "retry_quality_errors": retry_quality_errors,
                        "remaining_critical_errors": remaining_critical_errors,
                        "retry_payload": retry_profile.model_dump(mode="json"),
                        "debug_paths": debug_paths,
                    },
                )

                raise ValueError(
                    "Resume extraction failed critical quality gate after correction retry: "
                    + "; ".join(remaining_critical_errors)
                )
        except ValidationError as exc:
            _dump_llm_artifact(
                resume_id=resume_id,
                stage="07_retry_failed_schema_validation",
                payload={
                    "first_quality_errors": first_quality_errors,
                    "retry_payload": retry_payload,
                    "validation_error": str(exc),
                    "debug_paths": debug_paths,
                },
            )
            raise ValueError(
                "Resume extraction failed quality gate and correction retry failed schema validation: "
                + str(exc)
            ) from exc

        merged_payload = _merge_retry_payload(
            first_profile.model_dump(mode="json"),
            retry_profile.model_dump(mode="json"),
            resume_id=resume_id,
            file_name=file_name,
            sha256=sha256,
            ocr_markdown_path=ocr_markdown_path,
            first_quality_errors=first_quality_errors,
            retry_quality_errors=retry_quality_errors,
            debug_paths=debug_paths,
        )

        final_debug_path = _dump_llm_artifact(
            resume_id=resume_id,
            stage="08_final_merged_payload",
            payload={"payload": merged_payload},
        )
        if final_debug_path:
            debug_paths.append(final_debug_path)
            merged_payload.setdefault("extraction_quality", {})["llm_debug_paths"] = debug_paths

        return self._validate_payload(
            merged_payload,
            resume_id=resume_id,
            file_name=file_name,
            sha256=sha256,
            ocr_markdown_path=ocr_markdown_path,
        )
