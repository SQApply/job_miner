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

class JobPortalCreateRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=160)
    listing_url: str = Field(min_length=8, max_length=4096)
    max_pages_per_run: int = Field(default=50, ge=1, le=250)
    max_jobs_per_run: int = Field(default=500, ge=1, le=2000)
    request_rate_limit_per_minute: int = Field(default=30, ge=1, le=120)
    crawl_timeout_seconds: int = Field(default=1800, ge=30, le=7200)
    schedule_expression: str | None = Field(default=None, max_length=120)


class JobPortalUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=160)
    listing_url: str | None = Field(default=None, min_length=8, max_length=4096)
    max_pages_per_run: int | None = Field(default=None, ge=1, le=250)
    max_jobs_per_run: int | None = Field(default=None, ge=1, le=2000)
    request_rate_limit_per_minute: int | None = Field(default=None, ge=1, le=120)
    crawl_timeout_seconds: int | None = Field(default=None, ge=30, le=7200)
    schedule_expression: str | None = Field(default=None, max_length=120)
    configuration_version: int | None = Field(default=None, ge=1)


class JobPortalRunRequest(BaseModel):
    # Full portal runs are bounded by the portal's persisted max_jobs_per_run.
    # This optional lower cap is useful for controlled admin refreshes.
    max_jobs: int | None = Field(default=None, ge=1, le=2000)
