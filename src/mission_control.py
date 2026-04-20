from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from .blueprint_hub import BlueprintHub
from .crawl.browser_lane import build_browser_config, detail_run_config
from .extract.model_lane import build_llm_strategy, parse_extracted_jobs
from .extract.promptforge import build_instruction
from .extract.validator import is_valid_job
from .gpu_monitor import query_gpu_snapshot
from .logger import build_session_logger
from .router import get_adapter
from .schemas import RunResult
from .store.storefront import save_jobs_json, save_run_summary


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


async def run_target(root: Path, target_id: str) -> RunResult:
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

        jobs = []
        for idx, job_url in enumerate(job_urls, start=1):
            session_logger.log(
                "extract_start",
                job_url=job_url,
                item_index=idx,
            )

            gpu_before = query_gpu_snapshot()
            session_logger.log(
                "gpu_before_extract",
                job_url=job_url,
                item_index=idx,
                gpu=gpu_before,
            )

            t0 = time.perf_counter()

            detail_session_id = f"{target_id}_detail_session"

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
                item_index=idx,
                elapsed_seconds=elapsed,
                gpu=gpu_after,
            )

            if not result.success:
                _save_failed_payload(root, target_id, _slugify(job_url), f"CRAWL FAILED: {result.error_message}")
                session_logger.log(
                    "extract_failed",
                    job_url=job_url,
                    item_index=idx,
                    elapsed_seconds=elapsed,
                    error_message=result.error_message,
                )
                continue

            raw_content = result.extracted_content
            job = parse_extracted_jobs(raw_content, job_url)

            if job is None:
                _save_failed_payload(root, target_id, _slugify(job_url), raw_content)
                session_logger.log(
                    "parse_failed",
                    job_url=job_url,
                    item_index=idx,
                    elapsed_seconds=elapsed,
                )
                continue

            if is_valid_job(job):
                jobs.append(job)
                session_logger.log(
                    "extract_saved",
                    job_url=job_url,
                    item_index=idx,
                    elapsed_seconds=elapsed,
                    title=job.title,
                )
            else:
                _save_failed_payload(root, target_id, _slugify(job_url), raw_content)
                session_logger.log(
                    "validation_failed",
                    job_url=job_url,
                    item_index=idx,
                    elapsed_seconds=elapsed,
                    parsed_title=job.title,
                )

    total_elapsed_seconds = round(time.perf_counter() - website_t0, 3)

    output_dir = root / system_config.output.dir
    output_path = save_jobs_json(output_dir, blueprint.output_file, jobs, run_session_id)

    summary_path = save_run_summary(
        output_dir,
        target_id=target_id,
        run_session_id=run_session_id,
        discovered_urls=len(job_urls),
        extracted_jobs=len(jobs),
        output_path=str(output_path),
        log_path=str(log_path),
        total_elapsed_seconds=total_elapsed_seconds,
    )

    session_logger.log(
        "session_complete",
        output_path=str(output_path),
        summary_path=str(summary_path),
        discovered_urls=len(job_urls),
        extracted_jobs=len(jobs),
        total_elapsed_seconds=total_elapsed_seconds,
    )

    return RunResult(
        target_id=target_id,
        output_path=str(output_path),
        summary_path=str(summary_path),
        discovered_urls=len(job_urls),
        extracted_jobs=len(jobs),
        total_elapsed_seconds=total_elapsed_seconds,
        jobs=jobs,
    )


async def run_fleet(root: Path) -> list[RunResult]:
    hub = BlueprintHub(root)
    results: list[RunResult] = []

    for target in hub.get_fleet_targets():
        results.append(await run_target(root, target.id))

    return results