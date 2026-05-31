from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import logging
import os

from ..warehouse.repositories import WarehouseRepository
from .embeddings import OllamaEmbedder
from .llm_reranker import LLMJobReranker
from .qdrant_store import QdrantVectorStore


try:
    from .matcher import build_match_record as _baseline_build_match_record
except Exception:
    _baseline_build_match_record = None


OPTIMIZED_MATCH_COLLECTION = "candidate_job_matches_llm_reranked"

LOG_LEVEL = os.getenv("JOB_MINER_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


def _utc_run_id(prefix: str) -> str:
    return prefix + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _candidate_id(candidate: dict[str, Any], index: int) -> str:
    return str(
        candidate.get("candidate_id")
        or candidate.get("resume_id")
        or candidate.get("sha256")
        or f"candidate_{index}"
    )


def _candidate_name(candidate: dict[str, Any]) -> str | None:
    return (
        candidate.get("full_name")
        or candidate.get("candidate_name")
        or candidate.get("name")
    )


def _candidate_text(candidate: dict[str, Any]) -> str:
    text = str(candidate.get("candidate_embedding_text") or "").strip()

    if text:
        return text

    parts = [
        candidate.get("identity_text"),
        candidate.get("skills_text"),
        candidate.get("experience_text"),
        candidate.get("education_text"),
    ]

    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def _merge_job_payload(
    *,
    qdrant_payload: dict[str, Any],
    mongo_job: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(qdrant_payload or {})

    if mongo_job:
        merged.update(mongo_job)

    if not merged.get("job_id"):
        merged["job_id"] = qdrant_payload.get("job_id")

    return merged


def _build_baseline_record(
    *,
    candidate: dict[str, Any],
    candidate_index: int,
    job_payload: dict[str, Any],
    qdrant_score: float,
    rank: int,
) -> dict[str, Any]:
    if _baseline_build_match_record is not None:
        try:
            return _baseline_build_match_record(
                candidate=candidate,
                candidate_index=candidate_index,
                job_payload=job_payload,
                qdrant_score=qdrant_score,
                rank=rank,
            )
        except Exception:
            pass

    return {
        "candidate_id": _candidate_id(candidate, candidate_index),
        "resume_id": candidate.get("resume_id"),
        "candidate_name": _candidate_name(candidate),
        "rank": rank,
        "job_id": job_payload.get("job_id"),
        "title": job_payload.get("title"),
        "company": job_payload.get("company"),
        "location_text": job_payload.get("location_text"),
        "job_url": job_payload.get("job_url"),
        "apply_url": job_payload.get("apply_url"),
        "score": round(float(qdrant_score), 6),
        "vector_score": round(float(qdrant_score), 6),
        "evidence": {},
    }


def _fallback_llm_score_from_baseline(baseline_score: float | None) -> int:
    if baseline_score is None:
        return 0

    min_useful = 0.45
    excellent = 0.72

    normalized = (baseline_score - min_useful) / (excellent - min_useful)
    normalized = max(0.0, min(1.0, normalized))

    return int(round(60 + normalized * 35))


def _upsert_optimized_matches(
    repo: WarehouseRepository,
    records: list[dict[str, Any]],
) -> None:
    """Save LLM-reranked matches into MongoDB without touching baseline matches.

    Important:
    MongoDB does not allow the same field in both $set and $setOnInsert.
    So created_at is removed from $set and only used in $setOnInsert.
    """

    if not records:
        logger.warning("No optimized LLM reranked matches to upsert.")
        return

    collection = repo.db[OPTIMIZED_MATCH_COLLECTION]

    logger.info(
        "Saving optimized LLM reranked matches collection=%s count=%s",
        OPTIMIZED_MATCH_COLLECTION,
        len(records),
    )

    for index, record in enumerate(records, start=1):
        set_payload = dict(record)

        created_at = set_payload.pop(
            "created_at",
            datetime.now(timezone.utc).isoformat(),
        )

        set_payload["updated_at"] = datetime.now(timezone.utc).isoformat()

        collection.update_one(
            {
                "match_run_id": record["match_run_id"],
                "candidate_id": record["candidate_id"],
                "job_id": record["job_id"],
            },
            {
                "$set": set_payload,
                "$setOnInsert": {
                    "created_at": created_at,
                },
            },
            upsert=True,
        )

        if index % 25 == 0:
            logger.info(
                "Saved optimized matches progress=%s/%s",
                index,
                len(records),
            )

    logger.info(
        "Finished saving optimized LLM reranked matches count=%s",
        len(records),
    )

def match_candidates_with_llm_rerank(
    *,
    repo: WarehouseRepository,
    store: QdrantVectorStore,
    embedder: OllamaEmbedder,
    reranker: LLMJobReranker,
    jobs_collection: str = "job_miner_jobs",
    top_k: int = 100,
    final_top_n: int = 10,
    output_dir: Path = Path("data/processed/matching_llm"),
    candidates_limit: int | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    logger.info(
        "Starting LLM reranked matching jobs_collection=%s top_k=%s final_top_n=%s",
        jobs_collection,
        top_k,
        final_top_n,
    )
    match_run_id = _utc_run_id("llm_rerank")

    if candidate_id:
        candidates = list(repo.db["candidate_tower_records"].find({"candidate_id": candidate_id}).limit(1))
    else:
        candidates = repo.candidate_towers(limit=candidates_limit)

    logger.info(
        "Fetched candidate tower records count=%s candidates_limit=%s candidate_id=%s",
        len(candidates),
        candidates_limit,
        candidate_id,
    )

    all_final_matches: list[dict[str, Any]] = []
    grouped_output: list[dict[str, Any]] = []
    unscored_llm_job_count = 0

    for candidate_index, candidate in enumerate(candidates):
        candidate_id = _candidate_id(candidate, candidate_index)
        logger.info(
            "Processing candidate index=%s candidate_id=%s",
            candidate_index,
            candidate_id,
        )
        candidate_text = _candidate_text(candidate)

        if not candidate_text:
            grouped_output.append(
                {
                    "candidate_id": candidate_id,
                    "resume_id": candidate.get("resume_id"),
                    "candidate_name": _candidate_name(candidate),
                    "status": "skipped_empty_candidate_embedding_text",
                    "matches": [],
                }
            )
            continue

        candidate_vector = embedder.embed_one(candidate_text)

        qdrant_results = store.search(
            jobs_collection,
            query_vector=candidate_vector,
            top_n=top_k,
        )
        logger.info(
            "Qdrant returned jobs candidate_id=%s count=%s",
            candidate_id,
            len(qdrant_results),
        )

        baseline_candidates: list[dict[str, Any]] = []

        for baseline_rank, result in enumerate(qdrant_results, start=1):
            qdrant_payload = result.payload or {}
            job_id = str(qdrant_payload.get("job_id") or "").strip()

            if not job_id:
                continue

            mongo_job = repo.get_job(job_id)
            job_payload = _merge_job_payload(
                qdrant_payload=qdrant_payload,
                mongo_job=mongo_job,
            )

            baseline_record = _build_baseline_record(
                candidate=candidate,
                candidate_index=candidate_index,
                job_payload=job_payload,
                qdrant_score=result.score,
                rank=baseline_rank,
            )

            job_payload["baseline_rank"] = baseline_rank
            job_payload["baseline_score"] = baseline_record.get("score")
            job_payload["vector_score"] = baseline_record.get("vector_score", result.score)
            job_payload["baseline_evidence"] = baseline_record.get("evidence") or {}

            baseline_candidates.append(job_payload)
        logger.info(
            "Calling LLM reranker candidate_id=%s baseline_candidates=%s",
            candidate_id,
            len(baseline_candidates),
        )
        llm_results = reranker.rerank(
            candidate=candidate,
            jobs=baseline_candidates,
        )
        logger.info(
            "LLM reranker returned candidate_id=%s llm_results=%s",
            candidate_id,
            len(llm_results),
        )

        llm_by_job_id = {
            item.job_id: item
            for item in llm_results
        }

        optimized_candidates: list[dict[str, Any]] = []

        candidate_unscored_count = 0

        for job_payload in baseline_candidates:
            job_id = str(job_payload.get("job_id") or "")
            baseline_score = job_payload.get("baseline_score")

            try:
                baseline_score_float = float(baseline_score)
            except Exception:
                baseline_score_float = None

            llm_result = llm_by_job_id.get(job_id)

            if not llm_result:
                # Candidate-facing optimized recommendations must be real LLM-scored records.
                # We no longer synthesize fallback LLM scores because that makes the product look
                # as if the LLM evaluated a job when it did not. Missing jobs are tracked in the
                # summary/logs and excluded from the optimized top-N output.
                candidate_unscored_count += 1
                continue

            optimized_candidates.append(
                {
                    "job_payload": job_payload,
                    "baseline_score_0_1": baseline_score_float,
                    "vector_score_0_1": job_payload.get("vector_score"),
                    "baseline_rank": job_payload.get("baseline_rank"),
                    "llm_match_score_0_100": llm_result.llm_match_score,
                    "final_score_0_100": llm_result.llm_match_score,
                    "llm_decision": llm_result.decision,
                    "llm_reason": llm_result.reason,
                    "llm_matched_skills": llm_result.matched_skills,
                    "llm_missing_skills": llm_result.missing_skills,
                    "llm_risk_flags": llm_result.risk_flags,
                    "reranker_status": "llm_scored",
                }
            )

        unscored_llm_job_count += candidate_unscored_count
        if candidate_unscored_count:
            logger.warning(
                "Dropped unscored LLM jobs from optimized output candidate_id=%s count=%s",
                candidate_id,
                candidate_unscored_count,
            )

        optimized_candidates.sort(
            key=lambda item: (
                item["final_score_0_100"],
                -(item["baseline_rank"] or 999999),
            ),
            reverse=True,
        )

        final_matches: list[dict[str, Any]] = []

        for final_rank, item in enumerate(optimized_candidates[:final_top_n], start=1):
            job_payload = item["job_payload"]
            now = datetime.now(timezone.utc).isoformat()

            record = {
                "match_run_id": match_run_id,
                "strategy": "qdrant_topk_llm_rerank",
                "candidate_id": candidate_id,
                "resume_id": candidate.get("resume_id"),
                "candidate_name": _candidate_name(candidate),
                "final_rank": final_rank,
                "job_id": job_payload.get("job_id"),
                "title": job_payload.get("title"),
                "company": job_payload.get("company"),
                "location_text": job_payload.get("location_text"),
                "job_url": job_payload.get("job_url"),
                "apply_url": job_payload.get("apply_url"),
                "baseline_rank": item["baseline_rank"],
                "baseline_score_0_1": item["baseline_score_0_1"],
                "vector_score_0_1": item["vector_score_0_1"],
                "llm_match_score_0_100": item["llm_match_score_0_100"],
                "final_score_0_100": item["final_score_0_100"],
                "score_source": "llm_reranker",
                "llm_decision": item["llm_decision"],
                "llm_reason": item["llm_reason"],
                "evidence": {
                    "llm_matched_skills": item["llm_matched_skills"],
                    "llm_missing_skills": item["llm_missing_skills"],
                    "llm_risk_flags": item["llm_risk_flags"],
                    "baseline_evidence": job_payload.get("baseline_evidence") or {},
                    "reranker_status": item["reranker_status"],
                },
                "created_at": now,
                "updated_at": now,
            }

            final_matches.append(record)
            all_final_matches.append(record)

        grouped_output.append(
            {
                "candidate_id": candidate_id,
                "resume_id": candidate.get("resume_id"),
                "candidate_name": _candidate_name(candidate),
                "retrieved_count": len(qdrant_results),
                "llm_scored_count": len(optimized_candidates),
                "unscored_llm_job_count": candidate_unscored_count,
                "final_top_n": final_top_n,
                "matches": final_matches,
            }
        )
    logger.info(
        "Prepared all optimized matches total=%s. Saving to MongoDB...",
        len(all_final_matches),
    )
    _upsert_optimized_matches(repo, all_final_matches)

    latest_json = output_dir / "candidate_job_matches_llm_latest.json"
    latest_jsonl = output_dir / "candidate_job_matches_llm_latest.jsonl"
    summary_path = output_dir / "candidate_job_matches_llm_latest_summary.json"

    _write_json(latest_json, grouped_output)
    _write_jsonl(latest_jsonl, all_final_matches)

    llm_scores = [
        float(item["final_score_0_100"])
        for item in all_final_matches
        if item.get("final_score_0_100") is not None
    ]

    baseline_scores = [
        float(item["baseline_score_0_1"])
        for item in all_final_matches
        if item.get("baseline_score_0_1") is not None
    ]

    summary = {
        "match_run_id": match_run_id,
        "strategy": "qdrant_topk_llm_rerank",
        "baseline_preserved": True,
        "baseline_collection": "candidate_job_matches",
        "optimized_collection": OPTIMIZED_MATCH_COLLECTION,
        "candidate_count": len(candidates),
        "matched_candidate_count": sum(1 for item in grouped_output if item.get("matches")),
        "total_match_count": len(all_final_matches),
        "llm_scored_candidate_job_count": len(all_final_matches),
        "unscored_llm_job_count": unscored_llm_job_count,
        "qdrant_top_k": top_k,
        "final_top_n": final_top_n,
        "embedding_model": embedder.model,
        "llm_reranker_model": reranker.model,
        "jobs_collection": jobs_collection,
        "average_llm_score_0_100": round(sum(llm_scores) / len(llm_scores), 6) if llm_scores else 0.0,
        "max_llm_score_0_100": round(max(llm_scores), 6) if llm_scores else 0.0,
        "average_baseline_score_0_1": round(sum(baseline_scores) / len(baseline_scores), 6) if baseline_scores else 0.0,
        "max_baseline_score_0_1": round(max(baseline_scores), 6) if baseline_scores else 0.0,
        "json_output_path": str(latest_json),
        "jsonl_output_path": str(latest_jsonl),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    _write_json(summary_path, summary)
    logger.info("Finished LLM reranked matching summary=%s", summary)
    return summary