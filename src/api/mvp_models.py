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
