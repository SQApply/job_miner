from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .base import MongoDocument, utc_now

RunStatus = Literal["started", "completed", "failed", "partial"]
EmbeddingStatus = Literal["pending", "indexed", "failed", "skipped"]


class WarehouseRunSessionDocument(MongoDocument):
    collection_name = "warehouse_run_sessions"
    pipeline_name: str
    run_session_id: str
    target_id: str | None = None
    status: RunStatus = "completed"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_paths: dict[str, str] = Field(default_factory=dict)
    error_message: str | None = None


class JobRawExtractionDocument(MongoDocument):
    collection_name = "job_raw_extractions"
    raw_id: str
    run_session_id: str | None = None
    target_id: str
    source_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str


class JobCurrentDocument(MongoDocument):
    collection_name = "jobs_current"
    job_id: str
    target_id: str
    source_url: str | None = None
    job_url: str | None = None
    apply_url: str | None = None
    title: str | None = None
    company: str | None = None
    location_text: str | None = None
    employment_type: str | None = None
    duration: str | None = None
    compensation_text: str | None = None
    summary: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    job_reference: str | None = None
    content_hash: str
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    last_run_session_id: str | None = None
    is_active: bool = True
    version: int = 1
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class JobHistoryDocument(MongoDocument):
    collection_name = "jobs_history"
    history_id: str
    job_id: str
    target_id: str
    source_url: str | None = None
    content_hash: str
    version: int
    payload: dict[str, Any] = Field(default_factory=dict)
    run_session_id: str | None = None


class ResumeProfileCurrentDocument(MongoDocument):
    collection_name = "resume_profiles_current"
    resume_id: str
    sha256: str
    source_file_name: str
    contact: dict[str, Any] = Field(default_factory=dict)
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
    experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    raw_ocr_markdown_path: str | None = None
    parse_warnings: list[str] = Field(default_factory=list)
    content_hash: str
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    last_run_session_id: str | None = None
    is_active: bool = True
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ResumeProfileHistoryDocument(MongoDocument):
    collection_name = "resume_profiles_history"
    history_id: str
    resume_id: str
    sha256: str
    source_file_name: str
    content_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    run_session_id: str | None = None


class JobTowerDocument(MongoDocument):
    collection_name = "job_tower_records"
    job_tower_id: str
    job_id: str
    target_id: str
    title: str | None = None
    company: str | None = None
    job_url: str | None = None
    apply_url: str | None = None
    location_text: str | None = None
    employment_type: str | None = None
    duration: str | None = None
    compensation_text: str | None = None
    summary: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    title_company_location_text: str
    requirements_text: str
    responsibilities_text: str
    compensation_embedding_text: str
    job_embedding_text: str
    source_content_hash: str
    embedding_status: EmbeddingStatus = "pending"
    embedding_model: str | None = None
    last_indexed_at: datetime | None = None


class CandidateTowerDocument(MongoDocument):
    collection_name = "candidate_tower_records"
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
    # Canonical skills used by embeddings, matching, and LLM reranking.
    # primary_skills/secondary_skills are kept as legacy fields for backward compatibility only.
    skills: list[str] = Field(default_factory=list)
    primary_skills: list[str] = Field(default_factory=list)
    secondary_skills: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    identity_text: str
    skills_text: str
    experience_text: str
    education_text: str
    candidate_embedding_text: str
    source_content_hash: str
    embedding_status: EmbeddingStatus = "pending"
    embedding_model: str | None = None
    last_indexed_at: datetime | None = None


class QdrantIndexStateDocument(MongoDocument):
    collection_name = "qdrant_index_state"
    index_id: str
    record_type: Literal["job", "candidate"]
    record_id: str
    collection_name_value: str
    embedding_model: str
    source_content_hash: str
    vector_size: int
    qdrant_point_id: str
    status: EmbeddingStatus = "indexed"
    indexed_at: datetime = Field(default_factory=utc_now)
    error_message: str | None = None


class CandidateJobMatchDocument(MongoDocument):
    collection_name = "candidate_job_matches"
    match_run_id: str
    candidate_id: str
    resume_id: str | None = None
    candidate_name: str | None = None
    job_id: str
    rank: int
    score: float
    vector_score: float
    title: str | None = None
    company: str | None = None
    location_text: str | None = None
    job_url: str | None = None
    apply_url: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
