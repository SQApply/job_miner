from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from ..api.recommendation_generation import generate_candidate_recommendations_after_upload
from ..infrastructure.celery_app import celery_app
from ..infrastructure.mongo import get_mongo_database
from .tracking import mark_task_completed, mark_task_failed, mark_task_running

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _set_recommendation_status(
    *,
    candidate_id: str,
    status: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Update candidate recommendation progress in MongoDB.

    This is intentionally best-effort. A status-update failure should not hide
    the actual task failure/success logs from Celery.
    """
    try:
        db = get_mongo_database()
        payload: dict[str, Any] = {
            "recommendation_status": status,
            "recommendation_status_message": message,
            "recommendation_updated_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        if extra:
            payload.update(extra)
        db["candidate_tower_records"].update_many(
            {"candidate_id": candidate_id},
            {"$set": payload},
        )
    except Exception:
        logger.exception("Failed to update recommendation status candidate_id=%s status=%s", candidate_id, status)


@celery_app.task(
    bind=True,
    name="src.tasks.recommendation_tasks.generate_recommendations_after_resume_upload_task",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=60 * 60,
    time_limit=70 * 60,
)
def generate_recommendations_after_resume_upload_task(
    self,
    *,
    candidate_id: str,
    resume_id: str | None = None,
    email: str | None = None,
    run_session_id: str | None = None,
    source: str = "candidate_resume_upload_celery",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Celery entrypoint for post-resume-upload recommendations.

    The existing recommendation pipeline is still implemented in
    src.api.recommendation_generation.generate_candidate_recommendations_after_upload.
    This task only moves execution out of FastAPI and into the Redis/Celery worker.
    """
    task_id = str(getattr(self.request, "id", "") or "")
    logger.info(
        "recommendation_task_started request_id=%s task_id=%s candidate_id=%s resume_id=%s email=%s",
        request_id,
        task_id,
        candidate_id,
        resume_id,
        email,
    )
    mark_task_running(
        task_uuid=task_id,
        message="Recommendation generation task started.",
        payload={
            "request_id": request_id,
            "candidate_id": candidate_id,
            "resume_id": resume_id,
            "run_session_id": run_session_id,
        },
    )

    _set_recommendation_status(
        candidate_id=candidate_id,
        status="running",
        message="Generating job recommendations in the background.",
        extra={
            "recommendation_task_id": task_id,
            "recommendation_request_id": request_id,
            "recommendation_run_session_id": run_session_id,
            "recommendation_started_at": _utc_now(),
            "recommendation_error": None,
        },
    )

    try:
        summary = generate_candidate_recommendations_after_upload(candidate_id, source=source)
        final_status = str(summary.get("status") or "completed")

        if final_status == "llm_ready":
            final_message = "AI-ranked job recommendations are ready."
        elif final_status == "baseline_ready":
            final_message = "Baseline job recommendations are ready."
        elif final_status == "no_matches":
            final_message = "Recommendation pipeline ran, but no matching jobs were found."
        elif final_status == "no_jobs":
            final_message = "No jobs are currently available for recommendations."
        else:
            final_message = f"Recommendation generation completed with status: {final_status}."

        _set_recommendation_status(
            candidate_id=candidate_id,
            status=final_status,
            message=final_message,
            extra={
                "recommendation_completed_at": _utc_now(),
                "recommendation_summary": summary,
                "recommendation_error": None,
            },
        )

        mark_task_completed(
            task_uuid=task_id,
            result={"status": final_status, **summary},
            message="Recommendation generation task completed.",
            event_payload={"request_id": request_id, "candidate_id": candidate_id, "resume_id": resume_id, "status": final_status},
        )
        logger.info(
            "recommendation_task_completed request_id=%s task_id=%s candidate_id=%s status=%s summary=%s",
            request_id,
            task_id,
            candidate_id,
            final_status,
            summary,
        )
        return summary

    except SoftTimeLimitExceeded:
        _set_recommendation_status(
            candidate_id=candidate_id,
            status="failed",
            message="Recommendation generation timed out.",
            extra={
                "recommendation_failed_at": _utc_now(),
                "recommendation_error": "soft_time_limit_exceeded",
            },
        )
        mark_task_failed(
            task_uuid=task_id,
            error=SoftTimeLimitExceeded(),
            entity_type="recommendation_generation",
            entity_id=candidate_id,
            failed_payload={"request_id": request_id, "candidate_id": candidate_id, "resume_id": resume_id},
            message="Recommendation generation task timed out.",
        )
        raise

    except Exception as exc:
        logger.exception("recommendation_task_failed request_id=%s task_id=%s candidate_id=%s", request_id, task_id, candidate_id)
        _set_recommendation_status(
            candidate_id=candidate_id,
            status="failed",
            message="Recommendation generation failed.",
            extra={
                "recommendation_failed_at": _utc_now(),
                "recommendation_error": repr(exc),
            },
        )
        mark_task_failed(
            task_uuid=task_id,
            error=exc,
            entity_type="recommendation_generation",
            entity_id=candidate_id,
            failed_payload={"request_id": request_id, "candidate_id": candidate_id, "resume_id": resume_id},
            message="Recommendation generation task failed.",
        )
        raise
