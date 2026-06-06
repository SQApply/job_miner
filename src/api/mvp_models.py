from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateLinkRequest(BaseModel):
    candidate_id: str


class JobActionRequest(BaseModel):
    match_run_id: str | None = None
    source_collection: str | None = None
    apply_url: str | None = None


class FeedbackRequest(BaseModel):
    job_id: str
    label: str = Field(pattern="^(good_match|bad_match|shortlisted|applied|rejected|irrelevant|neutral)$")
    match_run_id: str | None = None
    reason: str | None = None


class PipelineRunRequest(BaseModel):
    candidates_limit: int | None = None
    llm_top_k: int = 100
    final_top_n: int = 10
    run_llm: bool = True
    recreate_qdrant: bool = True


class CandidateProfileUpdateRequest(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    total_experience_years: float | None = None
    skills: list[str] | str | None = None
    domains: list[str] | str | None = None
    target_roles: list[str] | str | None = None
    preferred_locations: list[str] | str | None = None
    remote_preference: str | None = None
    employment_type: str | None = None
    seniority_level: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    notice_period: str | None = None
    expected_compensation: str | None = None
    summary: str | None = None
