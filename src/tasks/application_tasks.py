from __future__ import annotations

import logging
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from ..application_agent.services import ApplicationAgentService
from ..control.postgres import postgres_session
from ..control.repository import ControlRepository
from ..infrastructure.celery_app import APPLICATION_JOB_QUEUE, APPLICATION_ORCHESTRATION_QUEUE, celery_app
from ..observability.correlation import new_uuid
from .tracking import create_task_tracking_row, mark_task_completed, mark_task_failed, mark_task_running

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="src.tasks.application_tasks.run_application_batch_task",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=60 * 30,
    time_limit=60 * 35,
)
def run_application_batch_task(self, *, batch_id: str, request_id: str | None = None) -> dict[str, Any]:
    task_id = str(getattr(self.request, "id", "") or "")
    logger.info("application_batch_task_started request_id=%s task_id=%s batch_id=%s", request_id, task_id, batch_id)
    mark_task_running(
        task_uuid=task_id,
        message="Application batch orchestration started.",
        payload={"request_id": request_id, "batch_id": batch_id},
    )

    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            batch = repo.get_application_batch(batch_id)
            if not batch:
                raise ValueError(f"Application batch not found: {batch_id}")
            repo.set_application_batch_task(batch_id, task_id, langgraph_thread_id=f"application-batch-{batch_id}")
            repo.update_application_batch_status(batch_id, "running", metadata={"request_id": request_id})
            job_runs = repo.list_application_job_runs(batch_id)

        queued = 0
        for run in job_runs:
            run_status = str(run.get("run_status") or "")
            if run_status not in {"queued", "failed"}:
                continue
            job_task_id = new_uuid()
            payload = {
                "request_id": request_id,
                "batch_id": batch_id,
                "job_run_id": str(run["id"]),
                "candidate_id": str(run["candidate_id"]),
                "job_id": str(run["job_id"]),
            }
            create_task_tracking_row(
                task_uuid=job_task_id,
                task_name="src.tasks.application_tasks.run_single_job_application_task",
                queue_name=APPLICATION_JOB_QUEUE,
                user={"id": str(run["app_user_id"])},
                payload=payload,
            )
            with postgres_session() as session:
                ControlRepository(session).set_application_job_run_task(str(run["id"]), job_task_id, langgraph_thread_id=f"application-job-{run['id']}")
            run_single_job_application_task.apply_async(
                kwargs={"job_run_id": str(run["id"]), "request_id": request_id},
                queue=APPLICATION_JOB_QUEUE,
                task_id=job_task_id,
            )
            queued += 1

        with postgres_session() as session:
            repo = ControlRepository(session)
            batch = repo.refresh_application_batch_counts(batch_id) or repo.get_application_batch(batch_id)

        if batch and str(batch.get("batch_status")) in {"completed", "completed_with_failures"}:
            _finalize_if_batch_complete(batch_id=batch_id, request_id=request_id)

        result = {"status": "queued_job_runs", "batch_id": batch_id, "queued_job_runs": queued, "batch": batch}
        mark_task_completed(
            task_uuid=task_id,
            result=result,
            message="Application batch orchestration queued per-job runs.",
            event_payload={"request_id": request_id, "batch_id": batch_id, "queued_job_runs": queued},
        )
        return result

    except SoftTimeLimitExceeded as exc:
        _mark_batch_failed(batch_id=batch_id, request_id=request_id, task_id=task_id, error=exc)
        raise
    except Exception as exc:
        logger.exception("application_batch_task_failed request_id=%s task_id=%s batch_id=%s", request_id, task_id, batch_id)
        _mark_batch_failed(batch_id=batch_id, request_id=request_id, task_id=task_id, error=exc)
        raise


@celery_app.task(
    bind=True,
    name="src.tasks.application_tasks.run_single_job_application_task",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=60 * 10,
    time_limit=60 * 12,
)
def run_single_job_application_task(self, *, job_run_id: str, request_id: str | None = None) -> dict[str, Any]:
    task_id = str(getattr(self.request, "id", "") or "")
    logger.info("application_job_task_started request_id=%s task_id=%s job_run_id=%s", request_id, task_id, job_run_id)
    mark_task_running(
        task_uuid=task_id,
        message="Application job run started.",
        payload={"request_id": request_id, "job_run_id": job_run_id},
    )

    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            run = repo.get_application_job_run(job_run_id)
            if not run:
                raise ValueError(f"Application job run not found: {job_run_id}")
            repo.set_application_job_run_task(job_run_id, task_id, langgraph_thread_id=f"application-job-{job_run_id}")

        state = ApplicationAgentService(request_id=request_id).run_job_application(job_run_id=job_run_id)
        result = {
            "status": state.get("run_status"),
            "application_status": state.get("application_status"),
            "batch_id": state.get("batch_id"),
            "job_run_id": job_run_id,
            "job_id": state.get("job_id"),
            "message": state.get("message"),
            "error_type": state.get("error_type"),
        }
        mark_task_completed(
            task_uuid=task_id,
            result=result,
            message="Application job run completed.",
            event_payload={"request_id": request_id, **result},
        )

        batch_id = str(state.get("batch_id") or "")
        if batch_id:
            _finalize_if_batch_complete(batch_id=batch_id, request_id=request_id)
        return result

    except SoftTimeLimitExceeded as exc:
        _mark_job_failed(job_run_id=job_run_id, request_id=request_id, task_id=task_id, error=exc, message="Application job run timed out.")
        raise
    except Exception as exc:
        logger.exception("application_job_task_failed request_id=%s task_id=%s job_run_id=%s", request_id, task_id, job_run_id)
        _mark_job_failed(job_run_id=job_run_id, request_id=request_id, task_id=task_id, error=exc, message="Application job run failed.")
        raise


def _finalize_if_batch_complete(*, batch_id: str, request_id: str | None) -> None:
    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            batch = repo.refresh_application_batch_counts(batch_id)
            if not batch:
                return
            if str(batch.get("batch_status")) not in {"completed", "completed_with_failures"}:
                return
            existing = repo.list_candidate_notifications(str(batch["app_user_id"]), str(batch["candidate_id"]), limit=20)
            if any((n.get("payload") or {}).get("batch_id") == batch_id for n in existing):
                return
        ApplicationAgentService(request_id=request_id).finalize_batch(batch_id=batch_id)
    except Exception:
        logger.exception("application_batch_finalize_failed request_id=%s batch_id=%s", request_id, batch_id)


def _mark_batch_failed(*, batch_id: str, request_id: str | None, task_id: str, error: BaseException) -> None:
    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            repo.update_application_batch_status(batch_id, "failed", error_message=str(error), metadata={"request_id": request_id})
    finally:
        mark_task_failed(
            task_uuid=task_id,
            error=error,
            entity_type="application_batch",
            entity_id=batch_id,
            failed_payload={"request_id": request_id, "batch_id": batch_id},
            message="Application batch orchestration failed.",
        )


def _mark_job_failed(*, job_run_id: str, request_id: str | None, task_id: str, error: BaseException, message: str) -> None:
    batch_id: str | None = None
    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            run = repo.get_application_job_run(job_run_id)
            if run:
                batch_id = str(run["batch_id"])
                repo.update_application_job_run_status(
                    job_run_id,
                    "failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                    metadata={"request_id": request_id},
                )
                if run.get("application_id"):
                    repo.update_application_status_by_id(
                        str(run["application_id"]),
                        "agent_failed",
                        metadata={"request_id": request_id, "job_run_id": job_run_id, "error_message": str(error)},
                    )
                repo.refresh_application_batch_counts(batch_id)
    finally:
        mark_task_failed(
            task_uuid=task_id,
            error=error,
            entity_type="application_job_run",
            entity_id=job_run_id,
            failed_payload={"request_id": request_id, "job_run_id": job_run_id, "batch_id": batch_id},
            message=message,
        )
        if batch_id:
            _finalize_if_batch_complete(batch_id=batch_id, request_id=request_id)
