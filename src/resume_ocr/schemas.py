from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class ExperienceItem(BaseModel):
    company: str | None = None
    title: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool | None = None
    responsibilities: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    institution: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    score_or_grade: str | None = None


class ProjectItem(BaseModel):
    name: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    url: str | None = None


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
