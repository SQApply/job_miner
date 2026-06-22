from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..control.portal_repository import JobPortalRepository
from ..control.postgres import postgres_session
from ..infrastructure.celery_app import PORTAL_PROBE_QUEUE, PORTAL_SCRAPE_QUEUE
from ..portals.safety import PortalUrlSafetyError, validate_public_http_url
from ..tasks.portal_tasks import probe_job_portal_task, scrape_job_portal_task, test_scrape_job_portal_task
from .mvp_models import JobPortalCreateRequest, JobPortalRunRequest, JobPortalUpdateRequest
from .security import require_permission

router = APIRouter(prefix="/admin/job-portals", tags=["Admin Job Portals"])


def _is_platform_admin(user: dict[str, Any]) -> bool:
    return "platform_admin" in set(user.get("roles") or [])


def _org_id(user: dict[str, Any]) -> str:
    organization_id = str(user.get("organization_id") or "").strip()
    if not organization_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Authenticated user is missing an organization context.")
    return organization_id


def _public(portal: dict[str, Any]) -> dict[str, Any]:
    return JobPortalRepository.public_portal_payload(portal)


def _validated_portal_id(portal_id: str) -> str:
    try:
        return str(uuid.UUID(str(portal_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="portal_id must be a UUID.") from exc


def _get_portal_or_404(repo: JobPortalRepository, *, portal_id: str, user: dict[str, Any]) -> dict[str, Any]:
    portal_id = _validated_portal_id(portal_id)
    portal = repo.get_portal(
        portal_id=portal_id,
        organization_id=_org_id(user),
        platform_admin=_is_platform_admin(user),
    )
    if not portal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job portal was not found.")
    return portal


def _queue_task(
    *,
    portal: dict[str, Any],
    user: dict[str, Any],
    pipeline_name: str,
    task_name: str,
    queue_name: str,
    task_apply: Callable[..., Any],
    task_args_factory: Callable[[str], list[Any]],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist control rows before Celery enqueue, then enqueue after commit."""
    task_id = str(uuid.uuid4())
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        run = repo.create_portal_pipeline_run(
            portal=portal,
            pipeline_name=pipeline_name,
            user=user,
            metadata=metadata or {},
        )
        task = repo.control.create_task_row(
            task_uuid=task_id,
            task_name=task_name,
            queue_name=queue_name,
            pipeline_run_id=str(run["id"]),
            user=user,
            payload={"portal_id": str(portal["id"]), **(metadata or {})},
        )
        repo.control.add_task_event(
            task_id,
            "task_queued",
            "Portal task queued.",
            progress_percent=0,
            payload={"portal_id": str(portal["id"]), "pipeline_run_id": str(run["id"]), "queue_name": queue_name},
        )

    try:
        task_apply.apply_async(args=task_args_factory(str(run["id"])), queue=queue_name, task_id=task_id)
    except Exception as exc:
        with postgres_session() as session:
            repo = JobPortalRepository(session)
            repo.mark_portal_run_failed(portal_id=str(portal["id"]), error_message=str(exc))
            repo.control.complete_pipeline_run(str(run["id"]), "failed", error_message=str(exc))
            repo.control.update_task_status(task_id, "failed", error=exc)
            repo.control.add_task_event(task_id, "task_enqueue_failed", "Celery enqueue failed.", payload={"error": str(exc)})
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Portal task could not be queued. Check the Celery worker and Redis.") from exc

    return {"portal": _public(portal), "pipeline_run": run, "task": task, "task_id": task_id}


@router.get("")
def list_job_portals(
    limit: int = Query(default=100, ge=1, le=200),
    user: dict[str, Any] = Depends(require_permission("portal.view")),
) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        portals = repo.list_portals(
            organization_id=_org_id(user),
            platform_admin=_is_platform_admin(user),
            limit=limit,
        )
    return {"portals": [{**_public(portal), "latest_pipeline_status": portal.get("latest_pipeline_status"), "latest_pipeline_metrics": portal.get("latest_pipeline_metrics")} for portal in portals]}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_job_portal(
    payload: JobPortalCreateRequest,
    user: dict[str, Any] = Depends(require_permission("portal.manage")),
) -> dict[str, Any]:
    try:
        checked = validate_public_http_url(payload.listing_url)
    except PortalUrlSafetyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    with postgres_session() as session:
        repo = JobPortalRepository(session)
        portal = repo.create_portal(
            organization_id=_org_id(user),
            actor=user,
            display_name=payload.display_name,
            listing_url=checked.normalized_url,
            normalized_host=checked.hostname,
            allowed_hosts=list(checked.allowed_hosts),
            max_pages_per_run=payload.max_pages_per_run,
            max_jobs_per_run=payload.max_jobs_per_run,
            request_rate_limit_per_minute=payload.request_rate_limit_per_minute,
            crawl_timeout_seconds=payload.crawl_timeout_seconds,
            schedule_expression=payload.schedule_expression,
        )
    return _queue_task(
        portal=portal,
        user=user,
        pipeline_name="portal_probe",
        task_name="probe_job_portal_task",
        queue_name=PORTAL_PROBE_QUEUE,
        task_apply=probe_job_portal_task,
        task_args_factory=lambda run_id: [str(portal["id"]), run_id],
        metadata={"requested_action": "create_and_probe"},
    )


@router.get("/{portal_id}")
def get_job_portal(
    portal_id: str,
    user: dict[str, Any] = Depends(require_permission("portal.view")),
) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        portal = _get_portal_or_404(repo, portal_id=portal_id, user=user)
        runs = repo.list_portal_runs(portal_id=portal_id, limit=20)
    return {"portal": _public(portal), "runs": runs}


@router.patch("/{portal_id}")
def update_job_portal(
    portal_id: str,
    payload: JobPortalUpdateRequest,
    user: dict[str, Any] = Depends(require_permission("portal.manage")),
) -> dict[str, Any]:
    checked = None
    if payload.listing_url is not None:
        try:
            checked = validate_public_http_url(payload.listing_url)
        except PortalUrlSafetyError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    with postgres_session() as session:
        repo = JobPortalRepository(session)
        current = _get_portal_or_404(repo, portal_id=portal_id, user=user)
        try:
            updated = repo.update_portal(
                portal=current,
                actor=user,
                display_name=payload.display_name,
                listing_url=checked.normalized_url if checked else None,
                normalized_host=checked.hostname if checked else None,
                allowed_hosts=list(checked.allowed_hosts) if checked else None,
                max_pages_per_run=payload.max_pages_per_run,
                max_jobs_per_run=payload.max_jobs_per_run,
                request_rate_limit_per_minute=payload.request_rate_limit_per_minute,
                crawl_timeout_seconds=payload.crawl_timeout_seconds,
                schedule_expression=payload.schedule_expression,
                expected_configuration_version=payload.configuration_version,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"portal": _public(updated), "listing_url_changed": checked is not None}


@router.post("/{portal_id}/probe", status_code=status.HTTP_202_ACCEPTED)
def probe_existing_job_portal(
    portal_id: str,
    user: dict[str, Any] = Depends(require_permission("portal.run")),
) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        current = _get_portal_or_404(repo, portal_id=portal_id, user=user)
        if current["status"] == "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pause an active portal before re-probing it.")
        portal = repo.prepare_portal_for_probe(portal=current, actor=user)
    return _queue_task(
        portal=portal,
        user=user,
        pipeline_name="portal_probe",
        task_name="probe_job_portal_task",
        queue_name=PORTAL_PROBE_QUEUE,
        task_apply=probe_job_portal_task,
        task_args_factory=lambda run_id: [portal_id, run_id],
        metadata={"requested_action": "probe"},
    )


@router.post("/{portal_id}/test-scrape", status_code=status.HTTP_202_ACCEPTED)
def test_scrape_job_portal(
    portal_id: str,
    user: dict[str, Any] = Depends(require_permission("portal.run")),
) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        current = _get_portal_or_404(repo, portal_id=portal_id, user=user)
        if current["status"] not in {"ready_for_test", "needs_review"}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Portal must complete a probe before test scraping.")
        if current.get("source_platform") == "blocked_or_protected":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This portal is blocked or protected and cannot be scraped automatically.")
        portal = repo.prepare_portal_for_test(portal=current, actor=user)
    return _queue_task(
        portal=portal,
        user=user,
        pipeline_name="portal_test_scrape",
        task_name="test_scrape_job_portal_task",
        queue_name=PORTAL_SCRAPE_QUEUE,
        task_apply=test_scrape_job_portal_task,
        task_args_factory=lambda run_id: [portal_id, run_id],
        metadata={"requested_action": "test_scrape"},
    )


@router.post("/{portal_id}/activate", status_code=status.HTTP_202_ACCEPTED)
def activate_job_portal(
    portal_id: str,
    payload: JobPortalRunRequest,
    user: dict[str, Any] = Depends(require_permission("portal.run")),
) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        current = _get_portal_or_404(repo, portal_id=portal_id, user=user)
        if current["status"] != "ready_for_activation":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A successful test scrape is required before activation.")
        portal = repo.set_portal_status(portal=current, actor=user, status="active", is_active=True, event_type="portal.activated")
    return _queue_task(
        portal=portal,
        user=user,
        pipeline_name="portal_initial_scrape",
        task_name="scrape_job_portal_task",
        queue_name=PORTAL_SCRAPE_QUEUE,
        task_apply=scrape_job_portal_task,
        task_args_factory=lambda run_id: [portal_id, run_id, payload.max_jobs],
        metadata={"requested_action": "activate_and_initial_scrape", "max_jobs": payload.max_jobs},
    )


@router.post("/{portal_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_active_job_portal(
    portal_id: str,
    payload: JobPortalRunRequest,
    user: dict[str, Any] = Depends(require_permission("portal.run")),
) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        portal = _get_portal_or_404(repo, portal_id=portal_id, user=user)
        if portal["status"] != "active" or not portal["is_active"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only active portals can run a catalog refresh.")
    return _queue_task(
        portal=portal,
        user=user,
        pipeline_name="portal_refresh",
        task_name="scrape_job_portal_task",
        queue_name=PORTAL_SCRAPE_QUEUE,
        task_apply=scrape_job_portal_task,
        task_args_factory=lambda run_id: [portal_id, run_id, payload.max_jobs],
        metadata={"requested_action": "manual_refresh", "max_jobs": payload.max_jobs},
    )


@router.post("/{portal_id}/pause")
def pause_job_portal(
    portal_id: str,
    user: dict[str, Any] = Depends(require_permission("portal.pause")),
) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        portal = _get_portal_or_404(repo, portal_id=portal_id, user=user)
        if portal["status"] == "paused":
            return {"portal": _public(portal)}
        paused = repo.set_portal_status(portal=portal, actor=user, status="paused", is_active=False, event_type="portal.paused")
    return {"portal": _public(paused)}
