from __future__ import annotations

import logging
from typing import Any

from ..control.postgres import postgres_session
from ..control.repository import ControlRepository
from ..observability.correlation import new_uuid

logger = logging.getLogger(__name__)


def create_task_tracking_row(
    *,
    task_uuid: str,
    task_name: str,
    queue_name: str,
    user: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    pipeline_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Create or refresh a Postgres celery_tasks row for a queued task.

    This is best-effort for runtime availability, but queue failures are still
    handled by the caller so candidate-facing Mongo state remains correct.
    """
    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            row = repo.create_task_row(
                task_uuid=task_uuid,
                task_name=task_name,
                queue_name=queue_name,
                pipeline_run_id=pipeline_run_id,
                user=user,
                payload=payload or {},
            )
            repo.add_task_event(
                task_uuid,
                "task_queued",
                "Task queued.",
                progress_percent=0,
                payload={"queue_name": queue_name, **(payload or {})},
            )
            return row
    except Exception:
        logger.exception("Failed to create task tracking row task_uuid=%s task_name=%s", task_uuid, task_name)
        return None


def mark_task_running(
    *,
    task_uuid: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            repo.update_task_status(task_uuid, "running", result={})
            repo.add_task_event(
                task_uuid,
                "task_started",
                message,
                progress_percent=5,
                payload=payload or {},
            )
    except Exception:
        logger.exception("Failed to mark task running task_uuid=%s", task_uuid)


def mark_task_completed(
    *,
    task_uuid: str,
    result: dict[str, Any] | None = None,
    message: str = "Task completed.",
    event_payload: dict[str, Any] | None = None,
) -> None:
    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            repo.update_task_status(task_uuid, "completed", result=result or {})
            repo.add_task_event(
                task_uuid,
                "task_completed",
                message,
                progress_percent=100,
                payload=event_payload or result or {},
            )
    except Exception:
        logger.exception("Failed to mark task completed task_uuid=%s", task_uuid)


def mark_task_failed(
    *,
    task_uuid: str,
    error: BaseException,
    entity_type: str,
    entity_id: str | None,
    failed_payload: dict[str, Any] | None = None,
    message: str = "Task failed.",
) -> None:
    try:
        with postgres_session() as session:
            repo = ControlRepository(session)
            repo.update_task_status(task_uuid, "failed", result={}, error=error)
            repo.add_task_event(
                task_uuid,
                "task_failed",
                message,
                progress_percent=None,
                payload={
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                    **(failed_payload or {}),
                },
            )
            repo.record_processing_failure(
                failure_id=new_uuid(),
                task_uuid=task_uuid,
                entity_type=entity_type,
                entity_id=entity_id,
                error=error,
                failed_payload=failed_payload or {},
            )
    except Exception:
        logger.exception("Failed to mark task failed task_uuid=%s", task_uuid)
