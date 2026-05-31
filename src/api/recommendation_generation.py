from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..infrastructure.mongo import get_mongo_database
from ..infrastructure.settings import load_app_settings
from ..matching.mongo_matcher import match_candidates_from_mongo
from ..matching.llm_reranker import LLMJobReranker
from ..matching.reranked_matcher import match_candidates_with_llm_rerank
from ..matching.mongo_qdrant_sync import (
    build_embedder,
    build_vector_store,
    index_candidate_towers,
    index_job_towers,
)
from ..warehouse.repositories import WarehouseRepository
from ..warehouse.tower_builders import build_job_tower_document

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer env var %s=%r. Using default=%s", name, raw, default)
        return default


def _optional_env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer env var %s=%r. Ignoring.", name, raw)
        return None


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _build_llm_reranker() -> LLMJobReranker:
    return LLMJobReranker(
        model=_env_str("JOB_MINER_LLM_RERANKER_MODEL", "qwen2.5:7b"),
        base_url=_env_str("JOB_MINER_OLLAMA_URL", "http://localhost:11434"),
        timeout_seconds=_env_int("JOB_MINER_LLM_RERANK_TIMEOUT_SECONDS", 900),
        chunk_size=_env_int("JOB_MINER_LLM_RERANK_CHUNK_SIZE", 3),
        num_ctx=_env_int("JOB_MINER_LLM_RERANK_NUM_CTX", 8192),
        num_predict=_env_int("JOB_MINER_LLM_RERANK_NUM_PREDICT", 4096),
    )


def _set_candidate_recommendation_state(
    *,
    repo: WarehouseRepository,
    candidate_id: str,
    status: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "recommendation_status": status,
        "recommendation_status_message": message,
        "recommendation_updated_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    if extra:
        payload.update(extra)

    repo.db["candidate_tower_records"].update_one(
        {"candidate_id": candidate_id},
        {"$set": payload},
    )


def _build_job_towers_if_needed(
    *,
    repo: WarehouseRepository,
    limit: int | None = None,
) -> dict[str, Any]:
    existing_count = repo.db["job_tower_records"].count_documents({})
    if existing_count > 0:
        return {
            "status": "skipped_existing_job_towers",
            "existing_job_tower_records": existing_count,
        }

    jobs = repo.active_jobs(limit=limit)
    built = 0
    skipped_empty_embedding_text = 0

    for job in jobs:
        doc = build_job_tower_document(job)
        if not doc.job_embedding_text.strip():
            skipped_empty_embedding_text += 1
            continue
        repo.upsert_job_tower(doc)
        built += 1

    return {
        "status": "built_job_towers",
        "active_jobs": len(jobs),
        "job_tower_records_built": built,
        "skipped_empty_embedding_text": skipped_empty_embedding_text,
    }


def generate_candidate_recommendations_after_upload(
    candidate_id: str,
    *,
    source: str = "candidate_resume_upload",
) -> dict[str, Any]:
    """Generate baseline recommendations automatically after resume upload.

    This function is intentionally safe to run in a FastAPI BackgroundTasks
    worker for the local MVP. For production, move the same function body into a
    Celery task or queue worker so long Qdrant/Ollama work does not share the API
    process.
    """
    started = time.perf_counter()
    settings = load_app_settings()
    db = get_mongo_database()
    repo = WarehouseRepository(db)

    top_n = _env_int("JOB_MINER_AUTO_RECOMMENDATION_TOP_N", 50)
    job_tower_limit = _optional_env_int("JOB_MINER_AUTO_JOB_TOWER_LIMIT")
    job_index_limit = _optional_env_int("JOB_MINER_AUTO_JOB_INDEX_LIMIT")
    index_jobs = _env_bool("JOB_MINER_AUTO_INDEX_JOBS_AFTER_UPLOAD", True)
    build_job_towers = _env_bool("JOB_MINER_AUTO_BUILD_JOB_TOWERS_IF_EMPTY", True)
    run_llm_rerank = _env_bool("JOB_MINER_AUTO_LLM_RERANK_AFTER_UPLOAD", False)
    llm_top_k = _env_int("JOB_MINER_AUTO_LLM_RERANK_TOP_K", 25)
    llm_final_top_n = _env_int("JOB_MINER_AUTO_LLM_RERANK_FINAL_TOP_N", 10)

    logger.info(
        "Auto recommendation generation started candidate_id=%s source=%s top_n=%s",
        candidate_id,
        source,
        top_n,
    )

    _set_candidate_recommendation_state(
        repo=repo,
        candidate_id=candidate_id,
        status="running",
        message="Generating recommendations.",
        extra={
            "recommendation_started_at": _utc_now(),
            "recommendation_source": source,
        },
    )

    try:
        candidate = repo.db["candidate_tower_records"].find_one({"candidate_id": candidate_id})
        if not candidate:
            raise ValueError(f"candidate_tower_records not found for candidate_id={candidate_id}")

        if not str(candidate.get("candidate_embedding_text") or "").strip():
            raise ValueError(f"candidate_embedding_text is empty for candidate_id={candidate_id}")

        jobs_current_count = repo.db["jobs_current"].count_documents({})
        if jobs_current_count <= 0:
            summary = {
                "status": "no_jobs",
                "candidate_id": candidate_id,
                "jobs_current_count": jobs_current_count,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
            _set_candidate_recommendation_state(
                repo=repo,
                candidate_id=candidate_id,
                status="no_jobs",
                message="No jobs are available for recommendations.",
                extra={"recommendation_summary": summary},
            )
            logger.warning("Auto recommendation generation skipped because jobs_current is empty summary=%s", summary)
            return summary

        build_job_towers_summary = None
        if build_job_towers:
            build_job_towers_summary = _build_job_towers_if_needed(
                repo=repo,
                limit=job_tower_limit,
            )

        store = build_vector_store(settings.vector)
        embedder = build_embedder(settings.vector)

        qdrant_health = store.healthcheck()
        if not qdrant_health.get("ok"):
            raise RuntimeError(f"Qdrant healthcheck failed: {qdrant_health}")

        job_index_summary = None
        if index_jobs:
            pending_job_tower_count = repo.db["job_tower_records"].count_documents(
                {"embedding_status": {"$ne": "indexed"}}
            )
            if pending_job_tower_count > 0:
                job_index_summary = index_job_towers(
                    repo=repo,
                    store=store,
                    embedder=embedder,
                    collection_name=settings.vector.jobs_collection,
                    recreate=False,
                    only_pending=True,
                    limit=job_index_limit,
                )
            else:
                job_index_summary = {
                    "status": "skipped_no_pending_jobs",
                    "pending_job_tower_count": 0,
                }

        candidate_index_summary = index_candidate_towers(
            repo=repo,
            store=store,
            embedder=embedder,
            collection_name=settings.vector.candidates_collection,
            recreate=False,
            only_pending=False,
            candidate_id=candidate_id,
        )

        baseline_summary = match_candidates_from_mongo(
            repo=repo,
            store=store,
            embedder=embedder,
            jobs_collection=settings.vector.jobs_collection,
            top_n=top_n,
            output_dir=Path("data/processed/matching"),
            candidate_id=candidate_id,
        )

        total_match_count = int(baseline_summary.get("total_match_count") or 0)

        llm_rerank_summary: dict[str, Any] | None = None
        llm_rerank_error: str | None = None

        if run_llm_rerank and total_match_count > 0:
            _set_candidate_recommendation_state(
                repo=repo,
                candidate_id=candidate_id,
                status="llm_reranking",
                message="Baseline recommendations are ready. Running LLM reranking.",
                extra={"recommendation_summary": {"baseline": baseline_summary}},
            )
            try:
                logger.info(
                    "Auto LLM reranking started candidate_id=%s top_k=%s final_top_n=%s",
                    candidate_id,
                    llm_top_k,
                    llm_final_top_n,
                )
                llm_rerank_summary = match_candidates_with_llm_rerank(
                    repo=repo,
                    store=store,
                    embedder=embedder,
                    reranker=_build_llm_reranker(),
                    jobs_collection=settings.vector.jobs_collection,
                    top_k=llm_top_k,
                    final_top_n=llm_final_top_n,
                    output_dir=Path("data/processed/matching_llm"),
                    candidate_id=candidate_id,
                )
                logger.info("Auto LLM reranking completed summary=%s", llm_rerank_summary)
            except Exception as exc:
                # Do not fail the upload or baseline recommendations if LLM reranking fails.
                llm_rerank_error = str(exc)
                logger.exception("Auto LLM reranking failed candidate_id=%s", candidate_id)

        llm_match_count = int((llm_rerank_summary or {}).get("total_match_count") or 0)

        if llm_match_count > 0:
            final_status = "llm_ready"
            final_message = f"Generated {total_match_count} baseline recommendations and {llm_match_count} LLM-reranked recommendations."
        else:
            final_status = "baseline_ready" if total_match_count > 0 else "no_matches"
            final_message = (
                f"Generated {total_match_count} baseline recommendations."
                if total_match_count > 0
                else "Recommendation pipeline ran but no matching jobs were found."
            )
            if run_llm_rerank and llm_rerank_error:
                final_message += f" LLM reranking failed: {llm_rerank_error}"

        summary = {
            "status": final_status,
            "candidate_id": candidate_id,
            "jobs_current_count": jobs_current_count,
            "build_job_towers": build_job_towers_summary,
            "job_index": job_index_summary,
            "candidate_index": candidate_index_summary,
            "baseline": baseline_summary,
            "llm_rerank_enabled": run_llm_rerank,
            "llm_rerank": llm_rerank_summary,
            "llm_rerank_error": llm_rerank_error,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

        _set_candidate_recommendation_state(
            repo=repo,
            candidate_id=candidate_id,
            status=final_status,
            message=final_message,
            extra={
                "recommendation_finished_at": _utc_now(),
                "recommendation_summary": summary,
            },
        )

        logger.info("Auto recommendation generation completed summary=%s", summary)
        return summary

    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        logger.exception("Auto recommendation generation failed candidate_id=%s", candidate_id)
        _set_candidate_recommendation_state(
            repo=repo,
            candidate_id=candidate_id,
            status="failed",
            message=str(exc),
            extra={
                "recommendation_failed_at": _utc_now(),
                "recommendation_error": str(exc),
                "recommendation_summary": {
                    "status": "failed",
                    "candidate_id": candidate_id,
                    "error": str(exc),
                    "elapsed_seconds": elapsed,
                },
            },
        )
        return {
            "status": "failed",
            "candidate_id": candidate_id,
            "error": str(exc),
            "elapsed_seconds": elapsed,
        }
