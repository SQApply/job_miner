from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..application_agent.services import ApplicationAgentService
from ..control.postgres import postgres_session
from ..control.repository import ControlRepository
from ..observability.correlation import new_uuid
from ..tasks.application_tasks import run_application_batch_task
from ..tasks.tracking import create_task_tracking_row, mark_task_failed
from .mongo_views import get_job_by_id
from .mvp_models import ApplySavedJobsAgentRequest, RetryApplicationBatchRequest
from .security import require_permission

router = APIRouter(prefix="/me", tags=["candidate-application-agent"])


def _require_candidate_link(user: dict[str, Any]) -> dict[str, Any]:
    with postgres_session() as session:
        repo = ControlRepository(session)
        link = repo.get_primary_candidate_link(str(user["id"]))
    if not link:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Candidate profile is not linked yet.")
    return link


def _attach_job_details(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        job_id = item.get("job_id")
        job = get_job_by_id(str(job_id)) if job_id and str(job_id) != "__batch__" else None
        if job:
            item["job"] = job
            item.setdefault("title", job.get("title"))
            item.setdefault("company", job.get("company"))
            item.setdefault("location_text", job.get("location_text"))
            item.setdefault("job_url", job.get("job_url"))
            item.setdefault("apply_url", job.get("apply_url") or job.get("job_url"))
        enriched.append(item)
    return enriched


def _queue_batch_task(*, batch: dict[str, Any], user: dict[str, Any], request_id: str) -> str:
    queue_name = os.getenv("JOB_MINER_APPLICATION_ORCHESTRATION_QUEUE", "application_orchestration_queue")
    task_id = new_uuid()
    batch_id = str(batch["id"])
    task_payload = {"request_id": request_id, "batch_id": batch_id, "candidate_id": str(batch["candidate_id"])}
    create_task_tracking_row(
        task_uuid=task_id,
        task_name="src.tasks.application_tasks.run_application_batch_task",
        queue_name=queue_name,
        user=user,
        payload=task_payload,
    )
    with postgres_session() as session:
        ControlRepository(session).set_application_batch_task(batch_id, task_id, langgraph_thread_id=f"application-batch-{batch_id}")
    try:
        run_application_batch_task.apply_async(
            kwargs={"batch_id": batch_id, "request_id": request_id},
            queue=queue_name,
            task_id=task_id,
        )
    except Exception as exc:
        with postgres_session() as session:
            ControlRepository(session).update_application_batch_status(batch_id, "failed", error_message=str(exc), metadata={"request_id": request_id})
        mark_task_failed(
            task_uuid=task_id,
            error=exc,
            entity_type="application_batch",
            entity_id=batch_id,
            failed_payload=task_payload,
            message="Application batch task could not be queued.",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application batch could not be queued. Check Redis and the application Celery worker.",
        ) from exc
    return task_id


@router.post("/saved-jobs/apply-agent", status_code=status.HTTP_202_ACCEPTED)
def apply_saved_jobs_with_agent(
    payload: ApplySavedJobsAgentRequest,
    user: dict[str, Any] = Depends(require_permission("applications.manage_self")),
) -> dict[str, Any]:
    link = _require_candidate_link(user)
    request_id = new_uuid()
    selected_job_ids = payload.job_ids if payload.mode == "selected_jobs" else None
    result = ApplicationAgentService(request_id=request_id).create_batch_for_saved_jobs(
        app_user_id=str(user["id"]),
        candidate_id=str(link["candidate_id"]),
        job_ids=selected_job_ids,
        max_jobs=payload.max_jobs,
        require_review_before_submit=payload.require_review_before_submit,
    )
    batch = result["batch"]
    job_runs = result.get("job_runs") or []

    if result.get("already_running"):
        return {
            "batch_id": str(batch["id"]),
            "status": batch.get("batch_status"),
            "already_running": True,
            "requested_job_count": batch.get("requested_job_count"),
            "message": "An application batch is already running for this candidate.",
        }

    if not job_runs:
        final = ApplicationAgentService(request_id=request_id).finalize_batch(batch_id=str(batch["id"]))
        return {
            "batch_id": str(batch["id"]),
            "status": final["batch"].get("batch_status"),
            "requested_job_count": 0,
            "message": "No eligible saved jobs were found for agent application.",
        }

    task_id = _queue_batch_task(batch=batch, user=user, request_id=request_id)
    return {
        "batch_id": str(batch["id"]),
        "task_id": task_id,
        "status": "queued",
        "requested_job_count": len(job_runs),
        "message": f"Application agent started for {len(job_runs)} saved jobs.",
    }


@router.get("/application-batches")
def list_application_batches(
    limit: int = Query(default=10, ge=1, le=50),
    user: dict[str, Any] = Depends(require_permission("applications.manage_self")),
) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        batches = repo.list_application_batches(str(user["id"]), str(link["candidate_id"]), limit=limit)
        notifications = repo.list_candidate_notifications(str(user["id"]), str(link["candidate_id"]), limit=limit)
    return {"batches": batches, "notifications": notifications}


@router.get("/application-batches/{batch_id}")
def get_application_batch(
    batch_id: str,
    user: dict[str, Any] = Depends(require_permission("applications.manage_self")),
) -> dict[str, Any]:
    _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        batch = repo.get_application_batch(batch_id, app_user_id=str(user["id"]))
        if not batch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application batch not found")
        job_runs = repo.list_application_job_runs(batch_id)
    return {"batch": batch, "jobs": _attach_job_details(job_runs)}


@router.post("/application-batches/{batch_id}/retry-failed", status_code=status.HTTP_202_ACCEPTED)
def retry_failed_application_batch(
    batch_id: str,
    payload: RetryApplicationBatchRequest,
    user: dict[str, Any] = Depends(require_permission("applications.manage_self")),
) -> dict[str, Any]:
    link = _require_candidate_link(user)
    with postgres_session() as session:
        repo = ControlRepository(session)
        batch = repo.get_application_batch(batch_id, app_user_id=str(user["id"]))
        if not batch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application batch not found")
        old_runs = repo.list_application_job_runs(batch_id)

    retryable_statuses = {"failed", "precheck_failed"}
    retry_job_ids = [str(row["job_id"]) for row in old_runs if str(row.get("run_status")) in retryable_statuses]
    if not retry_job_ids:
        return {"status": "no_retryable_jobs", "message": "No retryable failed jobs were found for this batch."}

    request_id = new_uuid()
    result = ApplicationAgentService(request_id=request_id).create_batch_for_saved_jobs(
        app_user_id=str(user["id"]),
        candidate_id=str(link["candidate_id"]),
        job_ids=retry_job_ids[: payload.max_jobs],
        max_jobs=payload.max_jobs,
    )
    new_batch = result["batch"]
    job_runs = result.get("job_runs") or []
    if not job_runs:
        return {"status": "no_eligible_retry_jobs", "message": "Failed jobs are no longer eligible for retry."}
    task_id = _queue_batch_task(batch=new_batch, user=user, request_id=request_id)
    return {
        "batch_id": str(new_batch["id"]),
        "task_id": task_id,
        "status": "queued",
        "requested_job_count": len(job_runs),
        "message": f"Retry batch started for {len(job_runs)} jobs.",
    }
