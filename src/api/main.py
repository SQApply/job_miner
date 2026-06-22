from __future__ import annotations

import logging
import os
import random
import time
import uuid
from typing import Any

from ..common.env import load_runtime_env

load_runtime_env()

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from ..common.constants import ApplicationStatuses, EnvironmentVariables, MongoCollections
from ..control.postgres import postgres_healthcheck, postgres_session
from ..control.repository import ControlRepository
from ..observability.correlation import new_uuid, reset_request_id, set_request_id
from ..infrastructure.mongo import healthcheck as mongo_healthcheck, get_mongo_database
from ..matching.feedback import add_feedback
from ..tasks.mvp_tasks import full_demo_pipeline_task, run_cli_task
from .mongo_views import candidate_exists, create_incomplete_candidate_profile_for_user, find_candidates_by_verified_email, get_candidate_profile, get_job_by_id, get_recommended_job, list_all_jobs_catalog, list_candidate_recommendations, warehouse_counts
from .mvp_models import CandidateLinkRequest, CandidateProfileUpdateRequest, FeedbackRequest, JobActionRequest, PipelineRunRequest
from .resume_upload import process_candidate_resume_upload
from .profile_edit import update_candidate_profile
from .portal_routes import router as portal_router
from .security import get_current_user, keycloak_public_config, require_permission

app = FastAPI(title="Job Miner API", version="1.0.0")
logger = logging.getLogger(__name__)
app.include_router(portal_router)

def _cors_origins() -> list[str]:
    raw = os.getenv(EnvironmentVariables.API_CORS_ORIGINS, "http://localhost:5173,http://127.0.0.1:5173")
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _should_write_api_request_log(*, method: str, path: str, status_code: int) -> bool:
    if not _env_bool("JOB_MINER_API_REQUEST_DB_LOGGING", True):
        return False

    normalized_path = path.rstrip("/") or "/"
    if method == "OPTIONS":
        return False
    if normalized_path in {"/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}:
        return False

    if status_code >= 500 and _env_bool("JOB_MINER_API_REQUEST_LOG_5XX", True):
        return True

    if method in {"POST", "PUT", "PATCH", "DELETE"} and _env_bool("JOB_MINER_API_REQUEST_LOG_MUTATIONS", True):
        return True

    if method == "GET" and not _env_bool("JOB_MINER_API_REQUEST_LOG_SUCCESSFUL_GETS", False):
        return False

    sample_rate = max(0.0, min(_env_float("JOB_MINER_API_REQUEST_LOG_SAMPLE_RATE", 0.05), 1.0))
    return random.random() < sample_rate


@app.middleware("http")
async def api_request_logger(request: Request, call_next):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    response = None
    error_message = None

    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    except Exception as exc:
        error_message = str(exc)
        raise

    finally:
        try:
            duration_ms = int((time.perf_counter() - started) * 1000)
            status_code = response.status_code if response is not None else 500
            path = request.url.path
            method = request.method.upper()

            # Skip very noisy/non-business requests.
            if method == "OPTIONS" or path in {"/health", "/docs", "/openapi.json", "/favicon.ico"}:
                should_write_db_log = False
            else:
                db_logging_enabled = os.getenv("JOB_MINER_API_REQUEST_DB_LOGGING", "true").lower() == "true"
                log_successful_gets = os.getenv("JOB_MINER_API_REQUEST_LOG_SUCCESSFUL_GETS", "false").lower() == "true"
                log_5xx = os.getenv("JOB_MINER_API_REQUEST_LOG_5XX", "true").lower() == "true"
                log_mutations = os.getenv("JOB_MINER_API_REQUEST_LOG_MUTATIONS", "true").lower() == "true"

                is_5xx = status_code >= 500
                is_mutation = method in {"POST", "PUT", "PATCH", "DELETE"}
                is_successful_get = method == "GET" and 200 <= status_code < 400

                should_write_db_log = (
                    db_logging_enabled
                    and (
                        (log_5xx and is_5xx)
                        or (log_mutations and is_mutation)
                        or (log_successful_gets and is_successful_get)
                    )
                )

            if should_write_db_log:
                with postgres_session() as session:
                    session.execute(
                        text(
                            """
                            INSERT INTO job_miner_control.api_request_logs (
                                request_id,
                                method,
                                path,
                                status_code,
                                duration_ms,
                                error_message,
                                ip_address,
                                user_agent
                            ) VALUES (
                                :request_id,
                                :method,
                                :path,
                                :status_code,
                                :duration_ms,
                                :error_message,
                                :ip_address,
                                :user_agent
                            )
                            ON CONFLICT (request_id) DO NOTHING
                            """
                        ),
                        {
                            "request_id": request_id,
                            "method": method,
                            "path": path,
                            "status_code": status_code,
                            "duration_ms": duration_ms,
                            "error_message": error_message,
                            "ip_address": request.client.host if request.client else None,
                            "user_agent": request.headers.get("user-agent"),
                        },
                    )

        except Exception:
            # Never allow observability/logging failure to break API response.
            pass

@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "api": {"ok": True},
        "postgres": postgres_healthcheck(),
        "mongodb": mongo_healthcheck(),
        "keycloak": keycloak_public_config(),
    }


@app.get("/auth/keycloak-config")
def auth_config() -> dict[str, str]:
    return keycloak_public_config()


# def _candidate_profile_state(link: dict[str, Any] | None) -> tuple[str, str]:
#     if not link:
#         return "blocked", "contact_support"
#     profile = get_candidate_profile(str(link["candidate_id"]))
#     tower = profile.get("candidate_tower") if profile else None
#     if not tower:
#         return "conflict", "contact_support"
#     if tower.get("onboarding_required") or tower.get("profile_state") == "incomplete":
#         return "incomplete", "complete_profile"
#     return "ready", "show_profile"

def _candidate_profile_state(link: dict[str, Any] | None) -> tuple[str, str]:
    if not link:
        return "blocked", "contact_support"

    profile = get_candidate_profile(str(link["candidate_id"]))
    tower = profile.get("candidate_tower") if profile else None

    if not tower:
        return "conflict", "contact_support"

    resume_upload_status = str(tower.get("resume_upload_status") or "").lower()
    profile_state = str(tower.get("profile_state") or "").lower()

    if resume_upload_status in {"queued", "processing", "profile_extracting"} or profile_state == "processing":
        return "processing", "wait_for_resume_processing"

    if resume_upload_status == "failed":
        return "incomplete", "retry_resume_upload"

    if tower.get("onboarding_required") or profile_state == "incomplete":
        return "incomplete", "complete_profile"

    return "ready", "show_profile"



def _candidate_processing_payload(link: dict[str, Any] | None) -> dict[str, Any]:
    if not link or not link.get("candidate_id"):
        return {}

    profile = get_candidate_profile(str(link["candidate_id"]))
    tower = profile.get("candidate_tower") if profile else None
    if not isinstance(tower, dict):
        return {}

    return {
        "resume_upload_status": tower.get("resume_upload_status"),
        "resume_upload_status_message": tower.get("resume_upload_status_message"),
        "resume_upload_error": tower.get("resume_upload_error"),
        "resume_upload_updated_at": tower.get("resume_upload_updated_at"),
        "resume_upload_failed_at": tower.get("resume_upload_failed_at"),
        "resume_processing_task_id": tower.get("resume_processing_task_id"),
        "active_resume_upload_id": tower.get("active_resume_upload_id"),
        "active_resume_file_name": tower.get("active_resume_file_name"),
        "recommendation_status": tower.get("recommendation_status"),
        "recommendation_status_message": tower.get("recommendation_status_message"),
        "recommendation_error": tower.get("recommendation_error"),
        "recommendation_task_id": tower.get("recommendation_task_id"),
    }

def _ensure_candidate_profile_link(user: dict[str, Any]) -> tuple[dict[str, Any] | None, str, str]:
    if not user.get("id"):
        return None, "blocked", "contact_support"

    with postgres_session() as session:
        repo = ControlRepository(session)
        link = repo.get_primary_candidate_link(str(user["id"]))

    if link:
        state, next_action = _candidate_profile_state(link)
        return link, state, next_action

    if not user.get("email") or not user.get("email_verified"):
        return None, "blocked", "verify_email"

    candidates = find_candidates_by_verified_email(str(user["email"]))
    if len(candidates) > 1:
        with postgres_session() as session:
            repo = ControlRepository(session)
            repo.add_audit_event(
                organization_id=str(user.get("organization_id")) if user.get("organization_id") else None,
                actor_app_user_id=str(user["id"]),
                actor_keycloak_user_id=str(user.get("login_keycloak_user_id") or user.get("keycloak_user_id") or ""),
                event_type="candidate.auto_link.conflict",
                entity_type="candidate_user_link",
                entity_id=str(user["id"]),
                after_payload={"email": user.get("email"), "candidate_ids": [c.get("candidate_id") for c in candidates]},
            )
        return None, "conflict", "contact_support"

    if len(candidates) == 1:
        candidate = candidates[0]
        candidate_id = str(candidate["candidate_id"])
        resume_id = candidate.get("resume_id")
        metadata = {"link_source": "auto_verified_email", "email": user.get("email")}
    else:
        candidate = create_incomplete_candidate_profile_for_user(user)
        candidate_id = str(candidate["candidate_id"])
        resume_id = candidate.get("resume_id")
        metadata = {"link_source": "created_incomplete_profile", "email": user.get("email")}

    with postgres_session() as session:
        repo = ControlRepository(session)
        link = repo.link_candidate(str(user["id"]), candidate_id, resume_id, metadata=metadata)
        repo.add_audit_event(
            organization_id=str(user.get("organization_id")) if user.get("organization_id") else None,
            actor_app_user_id=str(user["id"]),
            actor_keycloak_user_id=str(user.get("login_keycloak_user_id") or user.get("keycloak_user_id") or ""),
            event_type="candidate.auto_link.created",
            entity_type="candidate_user_link",
            entity_id=str(link["id"]),
            after_payload={"candidate_id": candidate_id, "resume_id": resume_id, **metadata},
        )

    state, next_action = _candidate_profile_state(link)
    return link, state, next_action


@app.get("/me")
def me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    roles = user.get("roles") or []
    if "candidate" in roles:
        link, profile_state, next_action = _ensure_candidate_profile_link(user)
    else:
        with postgres_session() as session:
            repo = ControlRepository(session)
            link = repo.get_primary_candidate_link(str(user["id"])) if user.get("id") else None
        profile_state = "admin" if {"platform_admin", "tenant_admin"}.intersection(roles) else "not_candidate"
        next_action = "show_admin" if profile_state == "admin" else "contact_support"
    return {
        "user": user,
        "candidate_link": link,
        "profile_state": profile_state,
        "next_action": next_action,
        "candidate_processing": _candidate_processing_payload(link),
    }


@app.post("/me/link-candidate")
def link_candidate_disabled(_: CandidateLinkRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    # Disabled for production safety. A candidate must not be able to attach an
    # arbitrary candidate_id to their account. /me now provisions automatically
    # from a verified email, or an admin can use /admin/users/{app_user_id}/link-candidate.
    raise HTTPException(status_code=status.HTTP_410_GONE, detail="Manual self-linking is disabled. Reload /me or contact support.")


@app.post("/admin/users/{app_user_id}/link-candidate")
def admin_link_candidate(
    app_user_id: str,
    payload: CandidateLinkRequest,
    user: dict[str, Any] = Depends(require_permission("admin.view")),
) -> dict[str, Any]:
    exists, resume_id = candidate_exists(payload.candidate_id)
    if not exists:
        raise HTTPException(status_code=404, detail="candidate_id not found in MongoDB candidate_tower_records")
    with postgres_session() as session:
        repo = ControlRepository(session)
        link = repo.link_candidate(app_user_id, payload.candidate_id, resume_id, metadata={"link_source": "admin_manual_repair", "admin_user_id": user.get("id")})
        repo.add_audit_event(
            organization_id=str(user.get("organization_id")) if user.get("organization_id") else None,
            actor_app_user_id=str(user.get("id")) if user.get("id") else None,
            actor_keycloak_user_id=str(user.get("login_keycloak_user_id") or user.get("keycloak_user_id") or ""),
            event_type="candidate.admin_link.created",
            entity_type="candidate_user_link",
            entity_id=str(link["id"]),
            after_payload={"target_app_user_id": app_user_id, "candidate_id": payload.candidate_id, "resume_id": resume_id},
        )
    return {"linked": True, "candidate_link": link}


def _require_candidate_link(user: dict[str, Any]) -> dict[str, Any]:
    with postgres_session() as session:
        repo = ControlRepository(session)
        link = repo.get_primary_candidate_link(str(user["id"])) if user.get("id") else None
    if not link:
        raise HTTPException(status_code=409, detail="Candidate profile is not ready for this user. Reload /me or contact support.")
    return link




@app.post("/me/resume")
def upload_my_resume(
    response: Response,
    request: Request,
    resume: UploadFile = File(...),
    user: dict[str, Any] = Depends(require_permission("candidate.view_self")),
) -> dict[str, Any]:
    current_link = _require_candidate_link(user)
    result = process_candidate_resume_upload(
        upload=resume,
        user=user,
        current_link=current_link,
        request_id=str(getattr(request.state, "request_id", "") or new_uuid()),
    )
    link = result["candidate_link"]
    profile_state, next_action = _candidate_profile_state(link)

    if result.get("queued"):
        response.status_code = status.HTTP_202_ACCEPTED

    return {
        **result,
        "profile_state": profile_state,
        "next_action": next_action,
    }


@app.get("/me/profile")
def my_profile(user: dict[str, Any] = Depends(require_permission("candidate.view_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    profile = get_candidate_profile(link["candidate_id"])
    if not profile:
        raise HTTPException(status_code=404, detail="Candidate profile not found")
    return {"candidate_link": link, **profile}




@app.patch("/me/profile")
def update_my_profile(payload: CandidateProfileUpdateRequest, response: Response, request: Request, user: dict[str, Any] = Depends(require_permission("candidate.view_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    result = update_candidate_profile(
        candidate_id=link["candidate_id"],
        app_user_id=str(user["id"]),
        user_email=user.get("email"),
        payload=payload.model_dump(exclude_unset=True),
        request_id=getattr(request.state, "request_id", None),
    )
    with postgres_session() as session:
        repo = ControlRepository(session)
        repo.add_audit_event(
            organization_id=str(user.get("organization_id")) if user.get("organization_id") else None,
            actor_app_user_id=str(user.get("id")),
            actor_keycloak_user_id=str(user.get("login_keycloak_user_id") or user.get("keycloak_user_id") or ""),
            event_type="candidate.profile_updated",
            entity_type="candidate",
            entity_id=link["candidate_id"],
            after_payload={
                "changed_fields": result.get("changed_fields") or [],
                "matching_impacting_fields_changed": result.get("matching_impacting_fields_changed") or [],
                "recommendations_refresh_required": result.get("recommendations_refresh_required"),
                "profile_version": result.get("profile_version"),
            },
        )
    if result.get("recommendations_refresh_required"):
        response.status_code = status.HTTP_202_ACCEPTED
    return result


@app.get("/me/recommendations")
def my_recommendations(source: str = "llm", limit: int = 50, user: dict[str, Any] = Depends(require_permission("recommendations.view_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    return {"candidate_id": link["candidate_id"], "source": source, "recommendations": list_candidate_recommendations(link["candidate_id"], source=source, limit=limit)}


@app.get("/me/recommendations/status")
def my_recommendation_status(user: dict[str, Any] = Depends(require_permission("recommendations.view_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    db = get_mongo_database()
    candidate_id = link["candidate_id"]
    tower = db[MongoCollections.CANDIDATE_TOWER_RECORDS].find_one(
        {"candidate_id": candidate_id},
        {
            "_id": 0,
            "candidate_id": 1,
            "recommendation_status": 1,
            "recommendation_status_message": 1,
            "recommendation_task_id": 1,
            "recommendation_queue": 1,
            "recommendation_queued_at": 1,
            "recommendation_started_at": 1,
            "recommendation_updated_at": 1,
            "recommendation_finished_at": 1,
            "recommendation_completed_at": 1,
            "recommendation_failed_at": 1,
            "recommendation_error": 1,
            "recommendation_summary": 1,
            "embedding_status": 1,
            "embedding_model": 1,
            "last_indexed_at": 1,
        },
    ) or {"candidate_id": candidate_id}

    return {
        "candidate_id": candidate_id,
        "status": tower.get("recommendation_status") or "not_started",
        "message": tower.get("recommendation_status_message") or "Recommendation generation has not started.",
        "task_id": tower.get("recommendation_task_id"),
        "queue": tower.get("recommendation_queue"),
        "queued_at": tower.get("recommendation_queued_at"),
        "started_at": tower.get("recommendation_started_at"),
        "updated_at": tower.get("recommendation_updated_at"),
        "completed_at": tower.get("recommendation_completed_at") or tower.get("recommendation_finished_at"),
        "failed_at": tower.get("recommendation_failed_at"),
        "error": tower.get("recommendation_error"),
        "candidate_tower": tower,
        "baseline_count": db[MongoCollections.CANDIDATE_JOB_MATCHES].count_documents({"candidate_id": candidate_id}),
        "llm_count": db[MongoCollections.CANDIDATE_JOB_MATCHES_LLM_RERANKED].count_documents({"candidate_id": candidate_id}),
    }


@app.get("/me/jobs/all")
def my_all_jobs_catalog(
    q: str | None = None,
    freshness: str | None = None,
    work_modes: list[str] = Query(default=[]),
    employment_types: list[str] = Query(default=[]),
    locations: list[str] = Query(default=[]),
    companies: list[str] = Query(default=[]),
    skills: list[str] = Query(default=[]),
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
    user: dict[str, Any] = Depends(require_permission("recommendations.view_self")),
) -> dict[str, Any]:
    """Browse the candidate-facing job catalog with dynamic facets and freshness filters."""
    link = _require_candidate_link(user)
    catalog = list_all_jobs_catalog(
        limit=limit,
        offset=offset,
        q=q,
        freshness=freshness,
        work_modes=work_modes,
        employment_types=employment_types,
        locations=locations,
        companies=companies,
        skills=skills,
        sort=sort,
    )
    return {"candidate_id": link["candidate_id"], **catalog}


@app.get("/me/recommendations/{job_id}")
def my_job_detail(job_id: str, source: str = "llm", user: dict[str, Any] = Depends(require_permission("recommendations.view_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    result = get_recommended_job(link["candidate_id"], job_id, source=source)
    if not result:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return result


@app.post("/me/jobs/{job_id}/save")
def save_job(job_id: str, payload: JobActionRequest, user: dict[str, Any] = Depends(require_permission("applications.manage_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        saved = repo.save_job(str(user["id"]), link["candidate_id"], job_id, match_run_id=payload.match_run_id, source_collection=payload.source_collection, status=ApplicationStatuses.SAVED)
        repo.add_application_event(app_user_id=str(user["id"]), candidate_id=link["candidate_id"], job_id=job_id, event_type="job_saved", payload=saved)
    return {"saved_job": saved}


@app.post("/me/jobs/{job_id}/select")
def select_job(job_id: str, payload: JobActionRequest, user: dict[str, Any] = Depends(require_permission("applications.manage_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        app_row = repo.create_or_update_application(str(user["id"]), link["candidate_id"], job_id, match_run_id=payload.match_run_id, apply_url=payload.apply_url, status=ApplicationStatuses.SELECTED)
        repo.add_application_event(app_user_id=str(user["id"]), candidate_id=link["candidate_id"], job_id=job_id, event_type="job_selected", application_id=str(app_row["id"]), payload=app_row)
    return {"application": app_row}


@app.post("/me/jobs/{job_id}/apply-click")
def apply_click(job_id: str, payload: JobActionRequest, user: dict[str, Any] = Depends(require_permission("applications.manage_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        app_row = repo.create_or_update_application(str(user["id"]), link["candidate_id"], job_id, match_run_id=payload.match_run_id, apply_url=payload.apply_url, status=ApplicationStatuses.APPLY_CLICKED)
        repo.add_application_event(app_user_id=str(user["id"]), candidate_id=link["candidate_id"], job_id=job_id, event_type="apply_button_clicked", application_id=str(app_row["id"]), payload=app_row)
    return {"application": app_row, "open_url": payload.apply_url}


@app.post("/me/jobs/{job_id}/not-interested")
def not_interested(job_id: str, payload: JobActionRequest, user: dict[str, Any] = Depends(require_permission("applications.manage_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        saved = repo.save_job(str(user["id"]), link["candidate_id"], job_id, match_run_id=payload.match_run_id, source_collection=payload.source_collection, status=ApplicationStatuses.NOT_INTERESTED)
        repo.add_application_event(app_user_id=str(user["id"]), candidate_id=link["candidate_id"], job_id=job_id, event_type="job_not_interested", payload=saved)
    return {"status": "not_interested", "saved_job": saved}


def _attach_job_details(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        job_id = item.get("job_id")
        job = get_job_by_id(str(job_id)) if job_id else None
        if job:
            item["job"] = job
            item.setdefault("title", job.get("title"))
            item.setdefault("company", job.get("company"))
            item.setdefault("location_text", job.get("location_text"))
            item.setdefault("job_url", job.get("job_url"))
            item.setdefault("apply_url", job.get("apply_url") or job.get("job_url"))
        enriched.append(item)
    return enriched


@app.get("/me/saved-jobs")
def saved_jobs(user: dict[str, Any] = Depends(require_permission("applications.manage_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        rows = repo.list_saved_jobs(str(user["id"]), link["candidate_id"])
    return {"saved_jobs": _attach_job_details(rows)}


@app.get("/me/applications")
def applications(user: dict[str, Any] = Depends(require_permission("applications.manage_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        rows = repo.list_applications(str(user["id"]), link["candidate_id"])
    return {"applications": _attach_job_details(rows)}


@app.post("/me/recommendations/{job_id}/feedback")
def feedback(job_id: str, payload: FeedbackRequest, user: dict[str, Any] = Depends(require_permission("feedback.create_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    db = get_mongo_database()
    result = add_feedback(
        db=db,
        candidate_id=link["candidate_id"],
        job_id=job_id,
        label=payload.label,
        match_run_id=payload.match_run_id,
        reason=payload.reason,
        source="candidate_portal",
        created_by=user.get("email") or user.get("keycloak_user_id"),
    )
    return result


@app.get("/admin/stats")
def admin_stats(user: dict[str, Any] = Depends(require_permission("admin.view"))) -> dict[str, Any]:
    return {"warehouse_counts": warehouse_counts()}


@app.post("/admin/pipeline/full/run")
def run_full_pipeline(payload: PipelineRunRequest, user: dict[str, Any] = Depends(require_permission("pipeline.run"))) -> dict[str, Any]:
    with postgres_session() as session:
        repo = ControlRepository(session)
        run = repo.create_pipeline_run(pipeline_name="full_demo_pipeline", user=user, metadata=payload.model_dump())
    task_id = str(uuid.uuid4())
    with postgres_session() as session:
        repo = ControlRepository(session)
        task = repo.create_task_row(task_uuid=task_id, task_name="full_demo_pipeline_task", queue_name="maintenance_queue", pipeline_run_id=str(run["id"]), user=user, payload=payload.model_dump())
    full_demo_pipeline_task.apply_async(args=[str(run["id"]), payload.model_dump()], queue="maintenance_queue", task_id=task_id)
    return {"pipeline_run": run, "task": task, "task_id": task_id}


@app.get("/admin/pipeline/runs")
def pipeline_runs(limit: int = 25, user: dict[str, Any] = Depends(require_permission("pipeline.view"))) -> dict[str, Any]:
    with postgres_session() as session:
        repo = ControlRepository(session)
        return {"runs": repo.list_pipeline_runs(limit=limit)}


@app.get("/admin/tasks/{task_id}")
def task_status(task_id: str, user: dict[str, Any] = Depends(require_permission("pipeline.view"))) -> dict[str, Any]:
    with postgres_session() as session:
        repo = ControlRepository(session)
        task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": task}
