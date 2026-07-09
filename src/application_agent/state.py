from __future__ import annotations

from typing import Any, TypedDict


class JobApplicationState(TypedDict, total=False):
    request_id: str | None
    batch_id: str
    job_run_id: str
    application_id: str | None
    app_user_id: str
    candidate_id: str
    job_id: str
    langgraph_thread_id: str
    job: dict[str, Any] | None
    candidate_profile: dict[str, Any] | None
    resume_profile: dict[str, Any] | None
    apply_url: str | None
    portal_domain: str | None
    strategy_key: str | None
    terminal: bool
    run_status: str | None
    application_status: str | None
    message: str | None
    error_type: str | None
    error_message: str | None
    external_confirmation_id: str | None
    metadata: dict[str, Any]
