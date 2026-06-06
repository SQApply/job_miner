from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from fastapi import HTTPException

from ..common.constants import MongoCollections
from ..infrastructure.celery_app import celery_app
from ..infrastructure.mongo import get_mongo_database
from .tracking import mark_task_completed, mark_task_failed, mark_task_running

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _candidate_id_from_link(current_link: dict[str, Any] | None) -> str | None:
    if not current_link:
        return None
    value = current_link.get("candidate_id")
    return str(value) if value else None


def _resume_id_from_link(current_link: dict[str, Any] | None) -> str | None:
    if not current_link:
        return None
    value = current_link.get("resume_id")
    return str(value) if value else None


def _set_resume_status(
    *,
    current_link: dict[str, Any] | None,
    upload_id: str,
    status: str,
    message: str,
    error_message: str | None = None,
    task_id: str | None = None,
    request_id: str | None = None,
) -> None:
    candidate_id = _candidate_id_from_link(current_link)
    resume_id = _resume_id_from_link(current_link)
    if not candidate_id:
        return

    now = _utc_now()
    payload: dict[str, Any] = {
        "resume_upload_status": status,
        "resume_upload_status_message": message,
        "resume_upload_updated_at": now,
        "active_resume_upload_id": upload_id,
    }

    if status in {"queued", "processing", "profile_extracting"}:
        payload["profile_state"] = "processing"
        payload["onboarding_required"] = True
    elif status == "failed":
        payload["profile_state"] = "incomplete"
        payload["onboarding_required"] = True
        payload["resume_upload_failed_at"] = now
    elif status == "completed":
        payload["profile_state"] = "ready"
        payload["onboarding_required"] = False
        payload["resume_upload_completed_at"] = now

    if error_message:
        payload["resume_upload_error"] = error_message[:4000]
    else:
        payload["resume_upload_error"] = None
    if task_id:
        payload["resume_processing_task_id"] = task_id
    if request_id:
        payload["resume_upload_request_id"] = request_id

    try:
        db = get_mongo_database()
        db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one({"candidate_id": candidate_id}, {"$set": payload})
        if resume_id:
            db[MongoCollections.RESUME_PROFILES_CURRENT].update_one({"resume_id": resume_id}, {"$set": payload})
    except Exception:
        logger.exception("Failed to update resume status candidate_id=%s status=%s", candidate_id, status)


@celery_app.task(
    bind=True,
    name="src.tasks.resume_tasks.process_candidate_resume_upload_task",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=60 * 60,
    time_limit=70 * 60,
)
def process_candidate_resume_upload_task(
    self,
    *,
    local_path: str,
    original_name: str,
    file_size_bytes: int,
    upload_id: str,
    user: dict[str, Any],
    current_link: dict[str, Any] | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Run heavy resume OCR/profile extraction outside FastAPI.

    Mongo remains the candidate-facing status source; Postgres celery_tasks,
    celery_task_events and processing_failures store operational task lifecycle.
    """
    task_id = str(getattr(self.request, "id", "") or "")
    candidate_id = _candidate_id_from_link(current_link)

    logger.info(
        "resume_task_started request_id=%s task_id=%s upload_id=%s candidate_id=%s local_path=%s user_id=%s",
        request_id,
        task_id,
        upload_id,
        candidate_id,
        local_path,
        user.get("id"),
    )
    mark_task_running(
        task_uuid=task_id,
        message="Resume processing task started.",
        payload={"request_id": request_id, "upload_id": upload_id, "candidate_id": candidate_id},
    )
    _set_resume_status(
        current_link=current_link,
        upload_id=upload_id,
        status="processing",
        message="Your resume is being processed in the background.",
        task_id=task_id,
        request_id=request_id,
    )

    try:
        from ..api.resume_upload import _process_candidate_resume_file_from_path

        result = _process_candidate_resume_file_from_path(
            local_path=Path(local_path),
            original_name=original_name,
            file_size_bytes=file_size_bytes,
            upload_id=upload_id,
            user=user,
            current_link=current_link,
            request_id=request_id,
        )
        summary = {
            "uploaded": True,
            "queued": False,
            "candidate_id": result.get("candidate_id"),
            "resume_id": result.get("resume_id"),
            "run_session_id": result.get("run_session_id"),
            "recommendation_generation": result.get("recommendation_generation"),
        }
        mark_task_completed(
            task_uuid=task_id,
            result=summary,
            message="Resume processing task completed.",
            event_payload={"request_id": request_id, "upload_id": upload_id, **summary},
        )
        logger.info(
            "resume_task_completed request_id=%s task_id=%s upload_id=%s candidate_id=%s resume_id=%s",
            request_id,
            task_id,
            upload_id,
            summary.get("candidate_id"),
            summary.get("resume_id"),
        )
        return summary

    except SoftTimeLimitExceeded as exc:
        _set_resume_status(
            current_link=current_link,
            upload_id=upload_id,
            status="failed",
            message="Resume processing timed out. Please try uploading again.",
            error_message="soft_time_limit_exceeded",
            task_id=task_id,
            request_id=request_id,
        )
        mark_task_failed(
            task_uuid=task_id,
            error=exc,
            entity_type="resume_upload",
            entity_id=candidate_id or upload_id,
            failed_payload={"request_id": request_id, "upload_id": upload_id, "local_path": local_path},
            message="Resume processing task timed out.",
        )
        raise

    except HTTPException as exc:
        detail = str(exc.detail or "Resume processing failed.")
        _set_resume_status(
            current_link=current_link,
            upload_id=upload_id,
            status="failed",
            message="Resume processing failed. Please check the file and upload again.",
            error_message=detail,
            task_id=task_id,
            request_id=request_id,
        )
        serializable_error = RuntimeError(detail)
        mark_task_failed(
            task_uuid=task_id,
            error=serializable_error,
            entity_type="resume_upload",
            entity_id=candidate_id or upload_id,
            failed_payload={
                "request_id": request_id,
                "upload_id": upload_id,
                "local_path": local_path,
                "http_status_code": exc.status_code,
            },
            message="Resume processing task failed.",
        )
        logger.warning(
            "resume_task_failed_http request_id=%s task_id=%s upload_id=%s status=%s detail=%s",
            request_id,
            task_id,
            upload_id,
            exc.status_code,
            detail,
        )
        raise serializable_error from exc

    except Exception as exc:
        _set_resume_status(
            current_link=current_link,
            upload_id=upload_id,
            status="failed",
            message="Resume processing failed. Please check the file and upload again.",
            error_message=repr(exc),
            task_id=task_id,
            request_id=request_id,
        )
        mark_task_failed(
            task_uuid=task_id,
            error=exc,
            entity_type="resume_upload",
            entity_id=candidate_id or upload_id,
            failed_payload={"request_id": request_id, "upload_id": upload_id, "local_path": local_path},
            message="Resume processing task failed.",
        )
        logger.exception("resume_task_failed request_id=%s task_id=%s upload_id=%s", request_id, task_id, upload_id)
        raise
