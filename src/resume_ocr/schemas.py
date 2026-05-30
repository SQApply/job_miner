from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class OcrSettings(BaseModel):
    backend: Literal["glmocr_sdk", "ollama_image"] = "glmocr_sdk"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "glm-ocr-optimized"
    glmocr_config_path: str = "configs/glm_ocr_ollama.yaml"
    layout_device: str | None = "cpu"
    prompt: str = "Text Recognition:"
    max_pages: int = 8
    pdf_dpi: int = 140
    max_image_dimension: int = 1400
    jpeg_quality: int = 88
    ocr_concurrency: int = 1
    min_native_text_chars: int = 1200
    prefer_native_pdf_text: bool = True
    request_timeout_seconds: int = 900


class LlmSettings(BaseModel):
    provider: str = "qwen2.5:3b"
    base_url: str = "http://127.0.0.1:11434"
    temperature: float = 0.0
    max_tokens: int = 4096
    num_ctx: int = 8192
    request_timeout_seconds: int = 600


class ParserSettings(BaseModel):
    enabled: bool = True
    min_markdown_chars: int = 200
    max_markdown_chars_for_extraction: int = 24000


class OutputSettings(BaseModel):
    dir: str = "data/processed"
    failed_dir: str = "data/failed"
    log_dir: str = "data/logs"


def _optional_string(value: Any) -> str | None:
    """Normalize LLM scalar output before Pydantic validation.

    Small local LLMs sometimes emit numeric years such as 2023 instead of
    "2023". The application schema stores dates as strings because resumes
    often contain partial dates such as "Jan 2023", "2023", "Present", or
    "2019 - 2023".
    """
    if value is None:
        return None

    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None

    if isinstance(value, bool):
        return str(value).lower()

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)

    return str(value).strip() or None


def _optional_float(value: Any) -> float | None:
    """Normalize LLM total-experience output before Pydantic validation."""
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int | float):
        return float(value)

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None

        import re

        match = re.search(r"\d+(?:\.\d+)?", cleaned)
        if match:
            return float(match.group(0))

    return None


def _string_list(value: Any) -> list[str]:
    """Normalize LLM list/string output into a clean list[str]."""
    if value is None:
        return []

    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]

    if isinstance(value, list):
        cleaned: list[str] = []
        for item in value:
            text = _optional_string(item)
            if text:
                cleaned.append(text)
        return cleaned

    text = _optional_string(value)
    return [text] if text else []


class SystemConfig(BaseModel):
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    parser: ParserSettings = Field(default_factory=ParserSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)


class OcrPage(BaseModel):
    page_number: int
    markdown: str
    image_path: str | None = None
    elapsed_seconds: float | None = None


class OcrDocument(BaseModel):
    source_path: str
    resume_id: str
    sha256: str
    file_name: str
    file_ext: str
    backend: str
    used_native_text: bool = False
    markdown: str
    pages: list[OcrPage] = Field(default_factory=list)
    raw_result: dict[str, Any] | list[Any] | str | None = None
    started_at: datetime
    completed_at: datetime
    elapsed_seconds: float


class ContactInfo(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None

    @field_validator(
        "full_name",
        "email",
        "phone",
        "location",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        mode="before",
    )
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> str | None:
        return _optional_string(value)


class ExtractedEvidence(BaseModel):
    source_section: str | None = None
    block_id: str | None = None
    evidence_text: str | None = None

    @field_validator("source_section", "block_id", "evidence_text", mode="before")
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> str | None:
        return _optional_string(value)


class ExperienceItem(BaseModel):
    company: str | None = None
    title: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool | None = None
    responsibilities: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    evidence: ExtractedEvidence | None = None

    @field_validator("company", "title", "location", "start_date", "end_date", mode="before")
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> str | None:
        return _optional_string(value)

    @field_validator("responsibilities", "technologies", mode="before")
    @classmethod
    def normalize_string_lists(cls, value: Any) -> list[str]:
        return _string_list(value)


class EducationItem(BaseModel):
    institution: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    score_or_grade: str | None = None
    evidence: ExtractedEvidence | None = None

    @field_validator(
        "institution",
        "degree",
        "field_of_study",
        "start_date",
        "end_date",
        "score_or_grade",
        mode="before",
    )
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> str | None:
        return _optional_string(value)


class ProjectItem(BaseModel):
    name: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    url: str | None = None
    evidence: ExtractedEvidence | None = None

    @field_validator("name", "description", "url", mode="before")
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> str | None:
        return _optional_string(value)

    @field_validator("technologies", mode="before")
    @classmethod
    def normalize_string_lists(cls, value: Any) -> list[str]:
        return _string_list(value)


class ResumeProfile(BaseModel):
    resume_id: str
    source_file_name: str
    sha256: str
    contact: ContactInfo = Field(default_factory=ContactInfo)
    headline: str | None = None
    summary: str | None = None
    total_experience_years: float | None = None
    current_title: str | None = None
    current_company: str | None = None
    primary_skills: list[str] = Field(default_factory=list)
    secondary_skills: list[str] = Field(default_factory=list)
    tools_and_platforms: list[str] = Field(default_factory=list)
    programming_languages: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    raw_ocr_markdown_path: str | None = None
    parse_warnings: list[str] = Field(default_factory=list)
    extraction_quality: dict[str, Any] = Field(default_factory=dict)

    @field_validator("headline", "summary", "current_title", "current_company", mode="before")
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> str | None:
        return _optional_string(value)

    @field_validator("total_experience_years", mode="before")
    @classmethod
    def normalize_optional_float(cls, value: Any) -> float | None:
        return _optional_float(value)

    @field_validator(
        "primary_skills",
        "secondary_skills",
        "tools_and_platforms",
        "programming_languages",
        "domains",
        "certifications",
        "languages",
        "parse_warnings",
        mode="before",
    )
    @classmethod
    def normalize_string_lists(cls, value: Any) -> list[str]:
        return _string_list(value)


class CandidateTowerRecord(BaseModel):
    candidate_id: str
    resume_id: str
    source_file_name: str
    sha256: str
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    total_experience_years: float | None = None
    skills: list[str] = Field(default_factory=list)
    primary_skills: list[str] = Field(default_factory=list)  # legacy only
    secondary_skills: list[str] = Field(default_factory=list)  # legacy only
    domains: list[str] = Field(default_factory=list)
    identity_text: str
    skills_text: str
    experience_text: str
    education_text: str
    candidate_embedding_text: str


class RunResult(BaseModel):
    run_session_id: str
    input_path: str
    attempted_files: int
    processed_files: int
    skipped_files: int
    failed_files: int
    output_dir: str
    candidate_tower_path: str | None = None
    summary_path: str
    elapsed_seconds: float
