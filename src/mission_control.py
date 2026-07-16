from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from .blueprint_hub import BlueprintHub
from .extract.promptforge import build_instruction
from .infrastructure.mongo import get_mongo_database
from .gpu_monitor import query_gpu_snapshot
from .logger import build_session_logger
from .portals.orchestrator import (
    ScrapeExecutionOptions,
    ScrapeOrchestrator,
    ScrapeOrchestratorHooks,
)
from .schemas import RunResult
from .warehouse.repositories import WarehouseRepository
from .store.storefront import (
    save_jobs_csv,
    save_jobs_json,
    save_jobs_markdown,
    save_run_summary,
)


def _slugify(url: str) -> str:
    return (
        url.replace("https://", "")
        .replace("http://", "")
        .replace("/", "_")
        .replace("?", "_")
        .replace("&", "_")
        .replace("=", "_")
    )[:180]


def _save_failed_payload(root: Path, target_id: str, slug: str, content) -> None:
    failed_dir = root / "data" / "failed" / target_id
    failed_dir.mkdir(parents=True, exist_ok=True)
    path = failed_dir / f"{slug}.txt"
    path.write_text(str(content), encoding="utf-8")


def _new_run_session_id(target_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{target_id}_{stamp}"


def _plan_incremental_rescrape(
    *,
    target_id: str,
    run_session_id: str,
    discovered_urls: list[str],
    force_detail_refresh: bool,
    incremental_rescrape: bool,
    deep_refresh_days: int,
) -> tuple[list[str], dict]:
    if not incremental_rescrape:
        return list(dict.fromkeys(discovered_urls)), {
            "status": "disabled",
            "discovered_urls": len(set(discovered_urls)),
            "urls_to_extract_count": len(set(discovered_urls)),
            "known_skipped": 0,
            "force_detail_refresh": bool(force_detail_refresh),
        }

    try:
        repo = WarehouseRepository(get_mongo_database())
        plan = repo.plan_detail_rescrape(
            target_id=target_id,
            run_session_id=run_session_id,
            discovered_urls=discovered_urls,
            force_detail_refresh=force_detail_refresh,
            deep_refresh_days=deep_refresh_days,
        )
        return list(plan.get("urls_to_extract") or []), {**plan, "status": "planned"}
    except Exception as exc:
        # Scraping should still work if MongoDB is temporarily unavailable; it
        # will simply fall back to the old full-detail scrape behavior.
        return list(dict.fromkeys(discovered_urls)), {
            "status": "fallback_full_scrape",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "discovered_urls": len(set(discovered_urls)),
            "urls_to_extract_count": len(set(discovered_urls)),
        }


def _pending_backfill_reconcile(*, incremental_rescrape: bool) -> dict:
    if not incremental_rescrape:
        return {"status": "disabled"}
    return {"status": "pending_backfill", "message": "Missing/inactive reconciliation runs during warehouse backfill."}


async def run_target(
    root: Path,
    target_id: str,
    *,
    force_detail_refresh: bool = False,
    incremental_rescrape: bool = True,
    deep_refresh_days: int = 14,
    max_jobs: int | None = None,
) -> RunResult:
    website_t0 = time.perf_counter()

    hub = BlueprintHub(root)
    system_config = hub.system
    blueprint = hub.get_target(target_id)
    instruction = build_instruction(blueprint)

    run_session_id = _new_run_session_id(target_id)
    session_logger = build_session_logger(root, target_id, run_session_id)
    log_path = root / "data" / "logs" / target_id / f"{run_session_id}.jsonl"

    session_logger.log(
        "session_start",
        page_url=blueprint.listing.page_url,
        adapter=blueprint.adapter,
        model=system_config.llm.provider,
    )

    orchestrator = ScrapeOrchestrator(
        blueprint=blueprint,
        system_config=system_config,
        run_session_id=run_session_id,
        instruction=instruction,
    )
    orchestration = await orchestrator.run(
        options=ScrapeExecutionOptions(
            detail_concurrency=system_config.browser.detail_extraction_concurrency,
            detail_retry_attempts=0,
            max_jobs=max(1, int(max_jobs)) if max_jobs is not None else None,
            fail_on_zero_discovery=True,
            session_prefix=f"{target_id}_detail_session",
        ),
        hooks=ScrapeOrchestratorHooks(
            plan_detail_urls=lambda discovered_urls: _plan_incremental_rescrape(
                target_id=target_id,
                run_session_id=run_session_id,
                discovered_urls=discovered_urls,
                force_detail_refresh=force_detail_refresh,
                incremental_rescrape=incremental_rescrape,
                deep_refresh_days=deep_refresh_days,
            ),
            on_event=lambda event, payload: session_logger.log(event, **payload),
            on_failed_payload=lambda job_url, payload: _save_failed_payload(
                root,
                target_id,
                _slugify(job_url),
                payload,
            ),
            adapter_session_logger=session_logger,
            gpu_snapshot=query_gpu_snapshot,
        ),
    )
    job_urls = orchestration.discovered_job_urls
    urls_to_extract = orchestration.attempted_job_urls
    jobs = orchestration.jobs
    rescrape_plan = orchestration.rescrape_plan

    total_elapsed_seconds = round(time.perf_counter() - website_t0, 3)
    lifecycle_reconcile = _pending_backfill_reconcile(incremental_rescrape=incremental_rescrape)
    extraction_failures = max(0, len(urls_to_extract) - len(jobs))
    run_status = "partial" if extraction_failures else "success"

    output_dir = root / system_config.output.dir
    output_path = save_jobs_json(output_dir, blueprint.output_file, jobs, run_session_id)

    save_jobs_csv(
        output_dir=output_dir,
        output_file=blueprint.output_file,
        jobs=jobs,
        run_session_id=run_session_id,
    )

    save_jobs_markdown(
        output_dir=output_dir,
        output_file=blueprint.output_file,
        jobs=jobs,
        run_session_id=run_session_id,
    )

    summary_path = save_run_summary(
        output_dir,
        target_id=target_id,
        run_session_id=run_session_id,
        discovered_urls=len(job_urls),
        extracted_jobs=len(jobs),
        output_path=str(output_path),
        log_path=str(log_path),
        total_elapsed_seconds=total_elapsed_seconds,
        attempted_urls=len(urls_to_extract),
        discovered_job_urls=job_urls,
        rescrape_plan=rescrape_plan,
        lifecycle_reconcile=lifecycle_reconcile,
        status=run_status,
        extraction_failures=extraction_failures,
    )

    session_logger.log(
        "session_complete",
        output_path=str(output_path),
        summary_path=str(summary_path),
        discovered_urls=len(job_urls),
        extracted_jobs=len(jobs),
        attempted_urls=len(urls_to_extract),
        skipped_existing=int(rescrape_plan.get("known_skipped") or 0),
        discovered_job_urls=job_urls,
        rescrape_plan=rescrape_plan,
        lifecycle_reconcile=lifecycle_reconcile,
        total_elapsed_seconds=total_elapsed_seconds,
        status=run_status,
        extraction_failures=extraction_failures,
    )

    return RunResult(
        target_id=target_id,
        status=run_status,
        output_path=str(output_path),
        summary_path=str(summary_path),
        discovered_urls=len(job_urls),
        extracted_jobs=len(jobs),
        attempted_urls=len(urls_to_extract),
        skipped_existing=int(rescrape_plan.get("known_skipped") or 0),
        discovered_job_urls=job_urls,
        rescrape_plan=rescrape_plan,
        lifecycle_reconcile=lifecycle_reconcile,
        total_elapsed_seconds=total_elapsed_seconds,
        jobs=jobs,
    )


async def run_fleet(
    root: Path,
    *,
    force_detail_refresh: bool = False,
    incremental_rescrape: bool = True,
    deep_refresh_days: int = 14,
    max_jobs: int | None = None,
    target_ids: list[str] | None = None,
) -> list[RunResult]:
    hub = BlueprintHub(root)
    results: list[RunResult] = []

    targets = [hub.get_target(target_id) for target_id in target_ids] if target_ids else hub.get_fleet_targets()
    for target in targets:
        try:
            results.append(await run_target(
                root,
                target.id,
                force_detail_refresh=force_detail_refresh,
                incremental_rescrape=incremental_rescrape,
                deep_refresh_days=deep_refresh_days,
                max_jobs=max_jobs,
            ))
        except Exception as exc:
            results.append(RunResult(
                target_id=target.id,
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
                output_path="",
                summary_path="",
                discovered_urls=0,
                attempted_urls=0,
                skipped_existing=0,
                extracted_jobs=0,
                total_elapsed_seconds=0.0,
                jobs=[],
            ))

    return results
