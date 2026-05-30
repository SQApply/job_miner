from __future__ import annotations

import os
import time
import uuid
from typing import Any

from ..common.env import load_runtime_env

load_runtime_env()

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from ..common.constants import ApplicationStatuses, EnvironmentVariables, MongoCollections
from ..control.postgres import postgres_healthcheck, postgres_session
from ..control.repository import ControlRepository
from ..infrastructure.mongo import healthcheck as mongo_healthcheck, get_mongo_database
from ..matching.feedback import add_feedback
from ..tasks.mvp_tasks import full_demo_pipeline_task, run_cli_task
from .mongo_views import candidate_exists, create_incomplete_candidate_profile_for_user, find_candidates_by_verified_email, get_candidate_profile, get_job_by_id, get_recommended_job, list_candidate_recommendations, warehouse_counts
from .mvp_models import CandidateLinkRequest, FeedbackRequest, JobActionRequest, PipelineRunRequest
from .security import get_current_user, keycloak_public_config, require_permission

app = FastAPI(title="Job Miner API", version="1.0.0")

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


@app.middleware("http")
async def api_request_logger(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    response = None
    error_message = None
    try:
        response = await call_next(request)
        return response
    except Exception as exc:
        error_message = str(exc)
        raise
    finally:
        try:
            duration_ms = int((time.perf_counter() - started) * 1000)
            with postgres_session() as session:
                session.execute(
                    text(
                        """
                        INSERT INTO job_miner_control.api_request_logs (
                            request_id, method, path, status_code, duration_ms, error_message, ip_address, user_agent
                        ) VALUES (:request_id, :method, :path, :status_code, :duration_ms, :error_message, :ip_address, :user_agent)
                        ON CONFLICT (request_id) DO NOTHING
                        """
                    ),
                    {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code if response else 500,
                        "duration_ms": duration_ms,
                        "error_message": error_message,
                        "ip_address": request.client.host if request.client else None,
                        "user_agent": request.headers.get("user-agent"),
                    },
                )
        except Exception:
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


def _candidate_profile_state(link: dict[str, Any] | None) -> tuple[str, str]:
    if not link:
        return "blocked", "contact_support"
    profile = get_candidate_profile(str(link["candidate_id"]))
    tower = profile.get("candidate_tower") if profile else None
    if not tower:
        return "conflict", "contact_support"
    if tower.get("onboarding_required") or tower.get("profile_state") == "incomplete":
        return "incomplete", "complete_profile"
    return "ready", "show_profile"


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


@app.get("/me/profile")
def my_profile(user: dict[str, Any] = Depends(require_permission("candidate.view_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    profile = get_candidate_profile(link["candidate_id"])
    if not profile:
        raise HTTPException(status_code=404, detail="Candidate profile not found")
    return {"candidate_link": link, **profile}


@app.get("/me/recommendations")
def my_recommendations(source: str = "llm", limit: int = 50, user: dict[str, Any] = Depends(require_permission("recommendations.view_self"))) -> dict[str, Any]:
    link = _require_candidate_link(user)
    return {"candidate_id": link["candidate_id"], "source": source, "recommendations": list_candidate_recommendations(link["candidate_id"], source=source, limit=limit)}


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
