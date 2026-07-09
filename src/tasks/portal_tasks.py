from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from ..control.portal_repository import JobPortalRepository
from ..control.postgres import postgres_session
from ..infrastructure.celery_app import PORTAL_SCRAPE_QUEUE, celery_app
from ..infrastructure.mongo import get_mongo_database
from ..infrastructure.settings import load_app_settings
from ..matching.mongo_qdrant_sync import build_embedder, build_vector_store, index_job_towers
from ..portals.runner import probe_portal, scrape_portal
from ..tasks.tracking import mark_task_completed, mark_task_failed
from ..warehouse.documents import WarehouseRunSessionDocument
from ..warehouse.repositories import WarehouseRepository
from ..warehouse.tower_builders import build_job_tower_document

logger = logging.getLogger(__name__)
REPO_ROOT = Path(os.getenv("JOB_MINER_REPO_ROOT", ".")).resolve()
TEST_SCRAPE_MAX_JOBS = int(os.getenv("JOB_MINER_PORTAL_TEST_SCRAPE_MAX_JOBS", "5"))
TEST_SCRAPE_MAX_PAGES = int(os.getenv("JOB_MINER_PORTAL_TEST_SCRAPE_MAX_PAGES", "3"))
TEST_SCRAPE_DETAIL_RETRIES = int(os.getenv("JOB_MINER_PORTAL_TEST_SCRAPE_DETAIL_RETRIES", "1"))
TEST_SCRAPE_TIMEOUT_SECONDS = int(os.getenv("JOB_MINER_PORTAL_TEST_SCRAPE_TIMEOUT_SECONDS", "600"))
PORTAL_SCHEDULER_BATCH_SIZE = int(os.getenv("JOB_MINER_PORTAL_SCHEDULER_BATCH_SIZE", "10"))
PORTAL_RECOMMENDATION_REFRESH_CANDIDATE_LIMIT = int(os.getenv("JOB_MINER_PORTAL_RECOMMENDATION_REFRESH_CANDIDATE_LIMIT", "250"))


def _run_with_portal_timeout(coroutine: Any, timeout_seconds: int) -> Any:
    """Bound an entire probe/scrape, not merely individual browser operations."""
    return asyncio.run(asyncio.wait_for(coroutine, timeout=max(30, int(timeout_seconds))))


def _pipeline_started(*, portal_id: str, pipeline_run_id: str, task_uuid: str, step_key: str, step_name: str) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        portal = repo.get_portal(portal_id=portal_id, organization_id=None, platform_admin=True)
        if not portal:
            raise RuntimeError("Job portal does not exist.")
        if not repo.acquire_lease(portal_id=portal_id, lease_token=task_uuid, seconds=int(portal["crawl_timeout_seconds"])):
            raise RuntimeError("Another portal run is already in progress for this source.")
        repo.control.start_pipeline_run(pipeline_run_id)
        repo.mark_portal_run_started(portal_id)
        repo.upsert_step(
            pipeline_run_id=pipeline_run_id,
            step_key=step_key,
            step_name=step_name,
            step_order=1,
            status="running",
        )
        repo.control.update_task_status(task_uuid, "running", result={})
        repo.control.add_task_event(task_uuid, "portal_run_started", step_name, progress_percent=5, payload={"portal_id": portal_id})
        return portal


def _pipeline_completed(*, portal_id: str, pipeline_run_id: str, task_uuid: str, lifecycle_status: str | None, metrics: dict[str, Any], step_key: str, step_name: str) -> dict[str, Any]:
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        portal = repo.mark_portal_run_completed(
            portal_id=portal_id,
            status=lifecycle_status or "",
            success=True,
            result=metrics,
        )
        repo.upsert_step(
            pipeline_run_id=pipeline_run_id,
            step_key=step_key,
            step_name=step_name,
            step_order=1,
            status="completed",
            metrics=metrics,
        )
        repo.control.complete_pipeline_run(pipeline_run_id, "completed", metrics=metrics)
        repo.release_lease(portal_id=portal_id, lease_token=task_uuid)
        return portal


def _pipeline_failed(*, portal_id: str, pipeline_run_id: str, task_uuid: str, error: BaseException, step_key: str, step_name: str) -> None:
    try:
        with postgres_session() as session:
            repo = JobPortalRepository(session)
            repo.mark_portal_run_failed(portal_id=portal_id, error_message=str(error))
            repo.upsert_step(
                pipeline_run_id=pipeline_run_id,
                step_key=step_key,
                step_name=step_name,
                step_order=1,
                status="failed",
                error_message=str(error),
            )
            repo.control.complete_pipeline_run(pipeline_run_id, "failed", error_message=str(error))
            repo.release_lease(portal_id=portal_id, lease_token=task_uuid)
    finally:
        mark_task_failed(
            task_uuid=task_uuid,
            error=error,
            entity_type="job_portal",
            entity_id=portal_id,
            failed_payload={"pipeline_run_id": pipeline_run_id},
            message=f"{step_name} failed.",
        )


def _ingest_jobs(*, portal: dict[str, Any], run_session_id: str, jobs: list[Any]) -> tuple[dict[str, Any], list[str]]:
    db = get_mongo_database()
    warehouse = WarehouseRepository(db)
    warehouse.upsert_run_session(
        WarehouseRunSessionDocument(
            _id=f"portalrun_{run_session_id}",
            pipeline_name="portal_scrape",
            run_session_id=run_session_id,
            target_id=str(portal["target_id"]),
            status="completed",
            metrics={"extracted_jobs": len(jobs), "portal_id": str(portal["id"])},
        )
    )

    totals = {"input": 0, "inserted": 0, "changed": 0, "unchanged": 0, "job_towers_upserted": 0}
    changed_job_ids: list[str] = []
    for job in jobs:
        payload = job.model_dump(mode="python")
        payload["source_portal_id"] = str(portal["id"])
        payload["source_portal_key"] = str(portal["portal_key"])
        payload["source_platform"] = str(portal.get("source_platform") or "unknown")
        job_id, inserted, changed = warehouse.upsert_job(
            payload,
            target_id=str(portal["target_id"]),
            run_session_id=run_session_id,
        )
        totals["input"] += 1
        if inserted:
            totals["inserted"] += 1
        elif changed:
            totals["changed"] += 1
        else:
            totals["unchanged"] += 1

        if inserted or changed:
            current = warehouse.get_job(job_id)
            if current:
                warehouse.upsert_job_tower(build_job_tower_document(current))
                totals["job_towers_upserted"] += 1
                changed_job_ids.append(job_id)
    return totals, sorted(set(changed_job_ids))


def _index_changed_jobs(*, job_ids: list[str]) -> dict[str, Any]:
    """Index only changed portal jobs while the portal run lease is still held.

    Keeping this inside the same portal task avoids a stale index task overwriting
    a newer portal refresh. Candidate recommendations are intentionally not
    recalculated here; that would fan out work across every candidate.
    """
    normalized_ids = sorted({str(job_id) for job_id in job_ids if str(job_id).strip()})
    if not normalized_ids:
        return {"status": "skipped_no_changed_jobs", "requested_job_ids": 0, "indexed_records": 0}

    settings = load_app_settings()
    warehouse = WarehouseRepository(get_mongo_database())
    store = build_vector_store(settings.vector)
    health = store.healthcheck()
    if not bool(health.get("ok")):
        raise RuntimeError(f"Qdrant healthcheck failed before portal indexing: {health}")

    return index_job_towers(
        repo=warehouse,
        store=store,
        embedder=build_embedder(settings.vector),
        collection_name=settings.vector.jobs_collection,
        recreate=False,
        only_pending=True,
        limit=len(normalized_ids),
        job_ids=normalized_ids,
    )


def _record_artifacts(*, portal: dict[str, Any], pipeline_run_id: str, artifacts: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not artifacts:
        return {"status": "skipped_no_artifacts", "artifact_count": 0}
    with postgres_session() as session:
        repo = JobPortalRepository(session)
        repo.record_file_artifacts(portal=portal, pipeline_run_id=pipeline_run_id, artifacts=artifacts)
    return {"status": "recorded", "artifact_count": len(artifacts)}


def _reconcile_lifecycle_after_ingestion(*, portal: dict[str, Any], run_session_id: str, discovered_urls: list[str] | None) -> dict[str, Any]:
    urls = list(discovered_urls or [])
    if not urls:
        return {"status": "skipped_no_discovered_urls", "missing_marked": 0, "deactivated": 0}
    warehouse = WarehouseRepository(get_mongo_database())
    return warehouse.reconcile_missing_jobs_after_discovery(
        target_id=str(portal["target_id"]),
        run_session_id=run_session_id,
        discovered_urls=urls,
        deactivate_after_misses=int(portal.get("deactivate_after_misses") or 2),
        min_discovery_coverage_ratio=float(portal.get("min_discovery_coverage_ratio") or 0.25),
        allow_empty_discovery=False,
    )


def _record_recommendation_refresh_policy(*, portal: dict[str, Any], run_session_id: str, changed_job_ids: list[str]) -> dict[str, Any]:
    warehouse = WarehouseRepository(get_mongo_database())
    return warehouse.record_recommendation_refresh_requests(
        portal_id=str(portal["id"]),
        target_id=str(portal["target_id"]),
        run_session_id=run_session_id,
        changed_job_ids=changed_job_ids,
        candidate_limit=PORTAL_RECOMMENDATION_REFRESH_CANDIDATE_LIMIT,
    )


@celery_app.task(bind=True, name="src.tasks.portal_tasks.probe_job_portal_task")
def probe_job_portal_task(self, portal_id: str, pipeline_run_id: str) -> dict[str, Any]:
    task_uuid = self.request.id
    step_key = "portal_probe"
    step_name = "Validate listing URL, detect portal profile, and discover sample job links"
    try:
        portal = _pipeline_started(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            step_key=step_key,
            step_name=step_name,
        )
        run_session_id = str(pipeline_run_id)
        probe_result = _run_with_portal_timeout(
            probe_portal(root=REPO_ROOT, portal=portal, run_session_id=run_session_id),
            int(portal["crawl_timeout_seconds"]),
        )
        metrics = probe_result.to_dict()
        metrics["artifacts"] = _record_artifacts(portal=portal, pipeline_run_id=pipeline_run_id, artifacts=probe_result.artifacts)
        with postgres_session() as session:
            repo = JobPortalRepository(session)
            updated = repo.save_probe_result(portal_id=portal_id, result=metrics)
            repo.control.add_task_event(
                task_uuid,
                "portal_probe_completed",
                "Portal detection completed.",
                progress_percent=90,
                payload={"portal_id": portal_id, **metrics},
            )
        lifecycle_status = str(updated["status"])
        portal_out = _pipeline_completed(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            lifecycle_status=lifecycle_status,
            metrics=metrics,
            step_key=step_key,
            step_name=step_name,
        )
        mark_task_completed(
            task_uuid=task_uuid,
            result={"portal": JobPortalRepository.public_portal_payload(portal_out), "probe": metrics},
            message="Portal probe completed.",
        )
        return {"portal": JobPortalRepository.public_portal_payload(portal_out), "probe": metrics}
    except Exception as exc:
        logger.exception("Portal probe failed portal_id=%s", portal_id)
        _pipeline_failed(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            error=exc,
            step_key=step_key,
            step_name=step_name,
        )
        raise


@celery_app.task(bind=True, name="src.tasks.portal_tasks.test_scrape_job_portal_task")
def test_scrape_job_portal_task(self, portal_id: str, pipeline_run_id: str) -> dict[str, Any]:
    task_uuid = self.request.id
    step_key = "portal_test_scrape"
    step_name = "Run bounded test scrape and validate extracted job records"
    try:
        portal = _pipeline_started(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            step_key=step_key,
            step_name=step_name,
        )
        
        # max_jobs = min(TEST_SCRAPE_MAX_JOBS, int(portal["max_jobs_per_run"]))
        # result = _run_with_portal_timeout(
        #     scrape_portal(
        #         root=REPO_ROOT,
        #         portal=portal,
        #         run_session_id=str(pipeline_run_id),
        #         max_jobs=max_jobs,
        #         incremental_rescrape=False,
        #         reconcile_lifecycle=False,
        #     ),
        #     int(portal["crawl_timeout_seconds"]),
        # )
        max_jobs = min(TEST_SCRAPE_MAX_JOBS, int(portal["max_jobs_per_run"]))

        test_portal = dict(portal)
        test_portal["max_pages_per_run"] = min(
            TEST_SCRAPE_MAX_PAGES,
            int(portal.get("max_pages_per_run") or TEST_SCRAPE_MAX_PAGES),
        )
        test_portal["detail_retry_attempts"] = min(
            int(portal.get("detail_retry_attempts") or 0),
            TEST_SCRAPE_DETAIL_RETRIES,
        )

        test_timeout = min(
            int(portal.get("crawl_timeout_seconds") or TEST_SCRAPE_TIMEOUT_SECONDS),
            TEST_SCRAPE_TIMEOUT_SECONDS,
        )

        result = _run_with_portal_timeout(
            scrape_portal(
                root=REPO_ROOT,
                portal=test_portal,
                run_session_id=str(pipeline_run_id),
                max_jobs=max_jobs,
                incremental_rescrape=False,
                reconcile_lifecycle=False,
            ),
            test_timeout,
        )
        metrics = result.metrics()
        metrics["artifacts"] = _record_artifacts(portal=portal, pipeline_run_id=pipeline_run_id, artifacts=result.artifacts)
        metrics["test_max_jobs"] = max_jobs
        metrics["test_max_pages"] = int(test_portal["max_pages_per_run"])
        metrics["test_detail_retries"] = int(test_portal["detail_retry_attempts"])
        metrics["test_timeout_seconds"] = test_timeout
        metrics["sample_titles"] = [job.title for job in result.extracted_jobs[:5] if job.title]
        successful = bool(result.extracted_jobs) and result.discovered_urls > 0
        lifecycle_status = "ready_for_activation" if successful else "needs_review"
        portal_out = _pipeline_completed(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            lifecycle_status=lifecycle_status,
            metrics=metrics,
            step_key=step_key,
            step_name=step_name,
        )
        mark_task_completed(
            task_uuid=task_uuid,
            result={"portal": JobPortalRepository.public_portal_payload(portal_out), "test_scrape": metrics},
            message="Portal test scrape completed." if successful else "Test scrape completed but did not yield a valid job; review is required.",
        )
        return {"portal": JobPortalRepository.public_portal_payload(portal_out), "test_scrape": metrics}
    except Exception as exc:
        logger.exception("Portal test scrape failed portal_id=%s", portal_id)
        _pipeline_failed(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            error=exc,
            step_key=step_key,
            step_name=step_name,
        )
        raise


@celery_app.task(bind=True, name="src.tasks.portal_tasks.scrape_job_portal_task")
def scrape_job_portal_task(self, portal_id: str, pipeline_run_id: str, max_jobs: int | None = None) -> dict[str, Any]:
    task_uuid = self.request.id
    step_key = "portal_catalog_ingestion"
    step_name = "Scrape active portal and ingest validated jobs into the catalog"
    try:
        portal = _pipeline_started(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            step_key=step_key,
            step_name=step_name,
        )
        if not bool(portal.get("is_active")):
            raise RuntimeError("Portal is not active. Run a test scrape and activate it before catalog ingestion.")
        effective_max_jobs = min(int(max_jobs or portal["max_jobs_per_run"]), int(portal["max_jobs_per_run"]))
        result = _run_with_portal_timeout(
            scrape_portal(
                root=REPO_ROOT,
                portal=portal,
                run_session_id=str(pipeline_run_id),
                max_jobs=effective_max_jobs,
                incremental_rescrape=True,
                reconcile_lifecycle=False,
            ),
            int(portal["crawl_timeout_seconds"]),
        )
        artifact_metrics = _record_artifacts(portal=portal, pipeline_run_id=pipeline_run_id, artifacts=result.artifacts)
        if not result.extracted_jobs and int(result.skipped_existing or 0) == 0:
            raise RuntimeError("Portal scrape completed without a valid job record. Existing catalog jobs were left unchanged.")
        with postgres_session() as session:
            repo = JobPortalRepository(session)
            run = repo.get_pipeline_run(pipeline_run_id)
            run_session_id = str((run or {}).get("run_session_id") or pipeline_run_id)
        ingestion, changed_job_ids = _ingest_jobs(portal=portal, run_session_id=run_session_id, jobs=result.extracted_jobs)
        lifecycle_reconcile = _reconcile_lifecycle_after_ingestion(portal=portal, run_session_id=run_session_id, discovered_urls=result.discovered_job_urls)
        recommendation_refresh = _record_recommendation_refresh_policy(portal=portal, run_session_id=run_session_id, changed_job_ids=changed_job_ids)

        with postgres_session() as session:
            repo = JobPortalRepository(session)
            repo.upsert_step(
                pipeline_run_id=pipeline_run_id,
                step_key="portal_incremental_index",
                step_name="Embed and upsert changed portal jobs into Qdrant",
                step_order=2,
                status="running",
            )
            repo.control.add_task_event(
                task_uuid,
                "portal_incremental_index_started",
                "Indexing changed portal jobs in Qdrant.",
                progress_percent=80,
                payload={"portal_id": portal_id, "changed_job_count": len(changed_job_ids)},
            )

        try:
            incremental_indexing = _index_changed_jobs(job_ids=changed_job_ids)
            with postgres_session() as session:
                repo = JobPortalRepository(session)
                repo.upsert_step(
                    pipeline_run_id=pipeline_run_id,
                    step_key="portal_incremental_index",
                    step_name="Embed and upsert changed portal jobs into Qdrant",
                    step_order=2,
                    status="completed",
                    metrics=incremental_indexing,
                )
                repo.control.add_task_event(
                    task_uuid,
                    "portal_incremental_index_completed",
                    "Changed portal jobs were indexed in Qdrant.",
                    progress_percent=95,
                    payload={"portal_id": portal_id, **incremental_indexing},
                )
        except Exception as index_error:
            # MongoDB catalog ingestion has already committed. Do not remove jobs or
            # deactivate a healthy source because the optional vector update failed.
            logger.exception("Portal incremental Qdrant indexing failed portal_id=%s", portal_id)
            incremental_indexing = {
                "status": "failed",
                "requested_job_ids": len(changed_job_ids),
                "error_type": type(index_error).__name__,
                "error_message": str(index_error),
            }
            with postgres_session() as session:
                repo = JobPortalRepository(session)
                repo.upsert_step(
                    pipeline_run_id=pipeline_run_id,
                    step_key="portal_incremental_index",
                    step_name="Embed and upsert changed portal jobs into Qdrant",
                    step_order=2,
                    status="partial",
                    metrics=incremental_indexing,
                    error_message=str(index_error),
                )
                repo.control.add_task_event(
                    task_uuid,
                    "portal_incremental_index_failed",
                    "Catalog ingestion completed, but Qdrant indexing needs attention.",
                    progress_percent=95,
                    payload={"portal_id": portal_id, **incremental_indexing},
                )

        metrics = {**result.metrics(), "artifacts": artifact_metrics, "ingestion": ingestion, "lifecycle_reconcile": lifecycle_reconcile, "incremental_indexing": incremental_indexing, "recommendation_refresh": recommendation_refresh}
        portal_out = _pipeline_completed(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            lifecycle_status="active",
            metrics=metrics,
            step_key=step_key,
            step_name=step_name,
        )
        mark_task_completed(
            task_uuid=task_uuid,
            result={"portal": JobPortalRepository.public_portal_payload(portal_out), "scrape": metrics},
            message="Portal scrape and catalog ingestion completed.",
        )
        return {"portal": JobPortalRepository.public_portal_payload(portal_out), "scrape": metrics}
    except Exception as exc:
        logger.exception("Portal catalog scrape failed portal_id=%s", portal_id)
        _pipeline_failed(
            portal_id=portal_id,
            pipeline_run_id=pipeline_run_id,
            task_uuid=task_uuid,
            error=exc,
            step_key=step_key,
            step_name=step_name,
        )
        raise


@celery_app.task(bind=True, name="src.tasks.portal_tasks.schedule_due_job_portals_task")
def schedule_due_job_portals_task(self, limit: int | None = None) -> dict[str, Any]:
    """Enqueue bounded refresh runs for active portals whose next_run_at is due.

    Safe to run from Celery beat or manually. It uses the per-portal lease so
    two schedulers cannot queue the same portal concurrently.
    """
    task_uuid = str(getattr(self.request, "id", "") or uuid.uuid4())
    batch_limit = max(1, min(int(limit or PORTAL_SCHEDULER_BATCH_SIZE), 100))
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with postgres_session() as session:
        repo = JobPortalRepository(session)
        due_portals = repo.list_due_portals(limit=batch_limit)

    for portal in due_portals:
        portal_id = str(portal["id"])
        scrape_task_id = str(uuid.uuid4())
        lease_seconds = int(portal.get("crawl_timeout_seconds") or 1800) + 900
        try:
            with postgres_session() as session:
                repo = JobPortalRepository(session)
                if not repo.acquire_lease(portal_id=portal_id, lease_token=scrape_task_id, seconds=lease_seconds):
                    skipped.append({"portal_id": portal_id, "reason": "lease_busy"})
                    continue
                run = repo.create_portal_pipeline_run(
                    portal=portal,
                    pipeline_name="portal_scheduled_refresh",
                    user=None,
                    metadata={"requested_action": "scheduled_refresh", "scheduler_task_id": task_uuid},
                )
                repo.control.create_task_row(
                    task_uuid=scrape_task_id,
                    task_name="scrape_job_portal_task",
                    queue_name=PORTAL_SCRAPE_QUEUE,
                    pipeline_run_id=str(run["id"]),
                    user=None,
                    payload={"portal_id": portal_id, "requested_action": "scheduled_refresh"},
                )
                repo.control.add_task_event(
                    scrape_task_id,
                    "task_queued_by_scheduler",
                    "Scheduled portal refresh queued.",
                    progress_percent=0,
                    payload={"portal_id": portal_id, "pipeline_run_id": str(run["id"]), "scheduler_task_id": task_uuid},
                )
                repo.set_portal_queued_by_scheduler(portal_id=portal_id, lease_token=scrape_task_id)
            scrape_job_portal_task.apply_async(
                args=[portal_id, str(run["id"]), None],
                queue=PORTAL_SCRAPE_QUEUE,
                task_id=scrape_task_id,
            )
            queued.append({"portal_id": portal_id, "task_id": scrape_task_id, "pipeline_run_id": str(run["id"])})
        except Exception as exc:
            logger.exception("Failed to enqueue scheduled portal refresh portal_id=%s", portal_id)
            try:
                with postgres_session() as session:
                    repo = JobPortalRepository(session)
                    repo.release_lease(portal_id=portal_id, lease_token=scrape_task_id)
                    repo.mark_portal_run_failed(portal_id=portal_id, error_message=f"scheduler_enqueue_failed: {exc}")
            except Exception:
                logger.exception("Failed to release portal scheduler lease portal_id=%s", portal_id)
            skipped.append({"portal_id": portal_id, "reason": "enqueue_failed", "error": str(exc)})

    return {"status": "completed", "due_count": len(due_portals), "queued_count": len(queued), "skipped_count": len(skipped), "queued": queued, "skipped": skipped}
