from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from .blueprint_hub import BlueprintHub
from .crawl.browser_lane import build_browser_config, detail_run_config, close_session
from .extract.model_lane import build_llm_strategy, parse_extracted_jobs
from .extract.promptforge import build_instruction
from .extract.validator import is_valid_job
from .infrastructure.mongo import get_mongo_database
from .gpu_monitor import query_gpu_snapshot
from .logger import build_session_logger
from .router import get_adapter
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
) -> RunResult:
    from crawl4ai import AsyncWebCrawler

    website_t0 = time.perf_counter()

    hub = BlueprintHub(root)
    system_config = hub.system
    blueprint = hub.get_target(target_id)
    adapter = get_adapter(blueprint)
    browser_config = build_browser_config(system_config.browser)
    instruction = build_instruction(blueprint)
    llm_strategy = build_llm_strategy(system_config.llm, instruction)

    run_session_id = _new_run_session_id(target_id)
    session_logger = build_session_logger(root, target_id, run_session_id)
    log_path = root / "data" / "logs" / target_id / f"{run_session_id}.jsonl"

    session_logger.log(
        "session_start",
        page_url=blueprint.listing.page_url,
        adapter=blueprint.adapter,
        model=system_config.llm.provider,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        job_urls = await adapter.discover_job_urls(
            crawler,
            blueprint,
            system_config,
            session_logger=session_logger,
        )

        session_logger.log(
            "discovery_complete",
            discovered_urls=len(job_urls),
        )

        urls_to_extract, rescrape_plan = _plan_incremental_rescrape(
            target_id=target_id,
            run_session_id=run_session_id,
            discovered_urls=job_urls,
            force_detail_refresh=force_detail_refresh,
            incremental_rescrape=incremental_rescrape,
            deep_refresh_days=deep_refresh_days,
        )
        session_logger.log("rescrape_plan_complete", **rescrape_plan)

        concurrency = system_config.browser.detail_extraction_concurrency
        semaphore = asyncio.Semaphore(concurrency)

        async def extract_one_job(job_url: str, item_index: int):
            async with semaphore:
                session_logger.log(
                    "extract_start",
                    job_url=job_url,
                    item_index=item_index,
                )

                gpu_before = query_gpu_snapshot()
                session_logger.log(
                    "gpu_before_extract",
                    job_url=job_url,
                    item_index=item_index,
                    gpu=gpu_before,
                )

                t0 = time.perf_counter()
                detail_session_id = f"{target_id}_detail_session_{item_index}"

                async def cleanup_detail_session() -> None:
                    try:
                        await close_session(crawler, detail_session_id)
                    except Exception as cleanup_exc:
                        session_logger.log(
                            "detail_session_cleanup_failed",
                            job_url=job_url,
                            item_index=item_index,
                            detail_session_id=detail_session_id,
                            error_message=str(cleanup_exc),
                        )

                try:
                    result = await crawler.arun(
                        url=job_url,
                        config=detail_run_config(
                            system_config.browser,
                            blueprint.detail.wait_for,
                            llm_strategy,
                            session_id=detail_session_id,
                        ),
                    )

                    elapsed = round(time.perf_counter() - t0, 3)

                    gpu_after = query_gpu_snapshot()
                    session_logger.log(
                        "gpu_after_extract",
                        job_url=job_url,
                        item_index=item_index,
                        elapsed_seconds=elapsed,
                        gpu=gpu_after,
                    )

                    if not result.success:
                        _save_failed_payload(
                            root,
                            target_id,
                            _slugify(job_url),
                            f"CRAWL FAILED: {result.error_message}",
                        )
                        session_logger.log(
                            "extract_failed",
                            job_url=job_url,
                            item_index=item_index,
                            elapsed_seconds=elapsed,
                            error_message=result.error_message,
                        )
                        return None

                    raw_content = result.extracted_content
                    job = parse_extracted_jobs(raw_content, job_url)

                    if job is None:
                        _save_failed_payload(root, target_id, _slugify(job_url), raw_content)
                        session_logger.log(
                            "parse_failed",
                            job_url=job_url,
                            item_index=item_index,
                            elapsed_seconds=elapsed,
                        )
                        return None

                    if is_valid_job(job):
                        session_logger.log(
                            "extract_saved",
                            job_url=job_url,
                            item_index=item_index,
                            elapsed_seconds=elapsed,
                            title=job.title,
                        )
                        return job

                    _save_failed_payload(root, target_id, _slugify(job_url), raw_content)
                    session_logger.log(
                        "validation_failed",
                        job_url=job_url,
                        item_index=item_index,
                        elapsed_seconds=elapsed,
                        parsed_title=job.title,
                    )
                    return None

                except Exception as exc:
                    elapsed = round(time.perf_counter() - t0, 3)
                    _save_failed_payload(
                        root,
                        target_id,
                        _slugify(job_url),
                        f"CRAWL EXCEPTION: {exc}",
                    )
                    session_logger.log(
                        "extract_exception",
                        job_url=job_url,
                        item_index=item_index,
                        elapsed_seconds=elapsed,
                        error_message=str(exc),
                    )
                    return None

                finally:
                    await cleanup_detail_session()

        session_logger.log(
            "parallel_extraction_start",
            total_job_urls=len(urls_to_extract),
            discovered_urls=len(job_urls),
            skipped_existing=int(rescrape_plan.get("known_skipped") or 0),
            concurrency=concurrency,
        )

        extraction_results = await asyncio.gather(
            *(
                extract_one_job(job_url, idx)
                for idx, job_url in enumerate(urls_to_extract, start=1)
            ),
            return_exceptions=True,
        )

        jobs = []

        for item in extraction_results:
            if isinstance(item, Exception):
                session_logger.log(
                    "extract_task_exception",
                    error_message=str(item),
                )
                continue

            if item is not None:
                jobs.append(item)

        session_logger.log(
            "parallel_extraction_complete",
            attempted_urls=len(urls_to_extract),
            discovered_urls=len(job_urls),
            extracted_jobs=len(jobs),
        )

    total_elapsed_seconds = round(time.perf_counter() - website_t0, 3)
    lifecycle_reconcile = _pending_backfill_reconcile(incremental_rescrape=incremental_rescrape)

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
    )

    return RunResult(
        target_id=target_id,
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
) -> list[RunResult]:
    hub = BlueprintHub(root)
    results: list[RunResult] = []

    for target in hub.get_fleet_targets():
        results.append(await run_target(
            root,
            target.id,
            force_detail_refresh=force_detail_refresh,
            incremental_rescrape=incremental_rescrape,
            deep_refresh_days=deep_refresh_days,
        ))

    return results