from __future__ import annotations

import logging
import os
import time
from typing import Any

from ..infrastructure.settings import VectorSettings
from ..warehouse.repositories import WarehouseRepository
from .embeddings import OllamaEmbedder
from .qdrant_store import QdrantVectorStore, stable_qdrant_point_id


LOG_LEVEL = os.getenv("JOB_MINER_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


def build_embedder(settings: VectorSettings) -> OllamaEmbedder:
    logger.info(
        "Building Ollama embedder model=%s ollama_url=%s batch_size=%s max_chars_per_text=%s",
        settings.embedding_model,
        settings.ollama_url,
        settings.embedding_batch_size,
        settings.max_chars_per_text,
    )

    return OllamaEmbedder(
        settings.embedding_model,
        settings.ollama_url,
        settings.embedding_batch_size,
        max_chars_per_text=settings.max_chars_per_text,
    )


def build_vector_store(settings: VectorSettings) -> QdrantVectorStore:
    logger.info(
        "Building Qdrant vector store qdrant_url=%s has_api_key=%s",
        settings.qdrant_url,
        bool(settings.qdrant_api_key),
    )

    return QdrantVectorStore(
        settings.qdrant_url,
        settings.qdrant_api_key,
    )


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


def _text_length_stats(texts: list[str]) -> dict[str, Any]:
    lengths = [len(text or "") for text in texts]

    if not lengths:
        return {
            "count": 0,
            "min_chars": 0,
            "max_chars": 0,
            "avg_chars": 0,
        }

    return {
        "count": len(lengths),
        "min_chars": min(lengths),
        "max_chars": max(lengths),
        "avg_chars": round(sum(lengths) / len(lengths), 2),
    }


def _fallback_point_ids(ids: list[str], point_ids: Any) -> list[str]:
    """Keep the pipeline working even if old QdrantVectorStore.upsert returns None.

    New QdrantVectorStore.upsert should return list[str].
    Older version returned None, which caused:
        TypeError: 'NoneType' object is not iterable
    """
    if isinstance(point_ids, list):
        return [str(point_id) for point_id in point_ids]

    logger.warning(
        "QdrantVectorStore.upsert returned %s instead of list[str]. "
        "Using deterministic fallback point IDs. You should still update qdrant_store.py later.",
        type(point_ids).__name__,
    )

    return [stable_qdrant_point_id(raw_id) for raw_id in ids]


def _validate_vectors(vectors: list[list[float]], expected_count: int) -> int:
    if not vectors:
        raise RuntimeError("Embedding generation returned zero vectors.")

    if len(vectors) != expected_count:
        raise RuntimeError(
            f"Embedding count mismatch. Expected {expected_count}, got {len(vectors)}."
        )

    vector_size = len(vectors[0])

    if vector_size <= 0:
        raise RuntimeError("Embedding vector size is zero.")

    for index, vector in enumerate(vectors):
        if len(vector) != vector_size:
            raise RuntimeError(
                f"Embedding vector size mismatch at index={index}. "
                f"Expected {vector_size}, got {len(vector)}."
            )

    return vector_size


def _job_payload(row: dict[str, Any], *, embedding_model: str) -> dict[str, Any]:
    return {
        "record_type": "job",
        "job_id": row.get("job_id"),
        "job_tower_id": row.get("job_tower_id"),
        "target_id": row.get("target_id"),
        "title": row.get("title"),
        "company": row.get("company"),
        "location_text": row.get("location_text"),
        "job_url": row.get("job_url"),
        "apply_url": row.get("apply_url"),
        "source_content_hash": row.get("source_content_hash"),
        "embedding_model": embedding_model,
        "required_skills": row.get("required_skills") or [],
        "preferred_skills": row.get("preferred_skills") or [],
    }


def _candidate_payload(row: dict[str, Any], *, embedding_model: str) -> dict[str, Any]:
    return {
        "record_type": "candidate",
        "candidate_id": row.get("candidate_id"),
        "resume_id": row.get("resume_id"),
        "candidate_name": row.get("full_name"),
        "email": row.get("email"),
        "phone": row.get("phone"),
        "location": row.get("location"),
        "current_title": row.get("current_title"),
        "current_company": row.get("current_company"),
        "total_experience_years": row.get("total_experience_years"),
        "source_content_hash": row.get("source_content_hash"),
        "embedding_model": embedding_model,
        "primary_skills": row.get("primary_skills") or [],
        "secondary_skills": row.get("secondary_skills") or [],
        "domains": row.get("domains") or [],
    }


def _mark_indexed_records(
    *,
    repo: WarehouseRepository,
    rows: list[dict[str, Any]],
    point_ids: list[str],
    record_type: str,
    collection_name: str,
    embedding_model: str,
    vector_size: int,
) -> None:
    if len(rows) != len(point_ids):
        raise RuntimeError(
            f"Cannot mark indexed records. rows={len(rows)} point_ids={len(point_ids)}"
        )

    logger.info(
        "Marking indexed records in MongoDB record_type=%s count=%s",
        record_type,
        len(rows),
    )

    for index, (row, point_id) in enumerate(zip(rows, point_ids), start=1):
        if record_type == "job":
            record_id = str(row["job_id"])
        elif record_type == "candidate":
            record_id = str(row["candidate_id"])
        else:
            raise ValueError(f"Unsupported record_type={record_type}")

        repo.mark_tower_indexed(
            record_type=record_type,
            record_id=record_id,
            collection_name=collection_name,
            embedding_model=embedding_model,
            source_content_hash=str(row.get("source_content_hash") or ""),
            vector_size=vector_size,
            qdrant_point_id=point_id,
        )

        if index % 100 == 0:
            logger.info(
                "Marked indexed records progress record_type=%s completed=%s total=%s",
                record_type,
                index,
                len(rows),
            )

    logger.info(
        "Finished marking indexed records record_type=%s count=%s",
        record_type,
        len(rows),
    )


def index_job_towers(
    *,
    repo: WarehouseRepository,
    store: QdrantVectorStore,
    embedder: OllamaEmbedder,
    collection_name: str,
    recreate: bool = False,
    only_pending: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()

    logger.info(
        "Starting job tower indexing collection=%s recreate=%s only_pending=%s limit=%s",
        collection_name,
        recreate,
        only_pending,
        limit,
    )

    rows_from_mongo = repo.job_towers(
        only_pending=only_pending,
        limit=limit,
    )

    logger.info(
        "Fetched job tower rows from MongoDB count=%s",
        len(rows_from_mongo),
    )

    rows: list[dict[str, Any]] = []
    skipped_empty_text = 0
    skipped_missing_id = 0

    for row in rows_from_mongo:
        job_id = str(row.get("job_id") or "").strip()
        text = str(row.get("job_embedding_text") or "").strip()

        if not job_id:
            skipped_missing_id += 1
            logger.warning(
                "Skipping job tower row because job_id is missing row_keys=%s",
                sorted(row.keys()),
            )
            continue

        if not text:
            skipped_empty_text += 1
            logger.warning(
                "Skipping job_id=%s because job_embedding_text is empty",
                job_id,
            )
            continue

        rows.append(row)

    logger.info(
        "Prepared indexable job rows input=%s indexable=%s skipped_empty_text=%s skipped_missing_id=%s",
        len(rows_from_mongo),
        len(rows),
        skipped_empty_text,
        skipped_missing_id,
    )

    if not rows:
        summary = {
            "record_type": "job",
            "collection_name": collection_name,
            "input_records": len(rows_from_mongo),
            "indexable_records": 0,
            "indexed_records": 0,
            "skipped_empty_text": skipped_empty_text,
            "skipped_missing_id": skipped_missing_id,
            "embedding_model": embedder.model,
            "status": "no_indexable_records",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

        logger.warning("No indexable job tower records found. Summary=%s", summary)
        return summary

    ids = [
        f"job:{row['job_id']}:{embedder.model}"
        for row in rows
    ]

    texts = [
        str(row["job_embedding_text"])
        for row in rows
    ]

    payloads = [
        _job_payload(row, embedding_model=embedder.model)
        for row in rows
    ]

    logger.info(
        "Job embedding text stats=%s",
        _text_length_stats(texts),
    )

    logger.info(
        "Generating job embeddings model=%s text_count=%s",
        embedder.model,
        len(texts),
    )

    embedding_started = time.perf_counter()

    try:
        vectors = embedder.embed_many(texts)
    except Exception:
        logger.exception(
            "Failed while generating job embeddings model=%s text_count=%s",
            embedder.model,
            len(texts),
        )
        raise

    embedding_elapsed = time.perf_counter() - embedding_started
    vector_size = _validate_vectors(vectors, len(texts))

    logger.info(
        "Generated job embeddings count=%s vector_size=%s elapsed_seconds=%.3f",
        len(vectors),
        vector_size,
        embedding_elapsed,
    )

    logger.info(
        "Ensuring Qdrant job collection collection=%s vector_size=%s recreate=%s",
        collection_name,
        vector_size,
        recreate,
    )

    store.ensure_collection(
        collection_name,
        vector_size=vector_size,
        recreate=recreate,
    )

    logger.info(
        "Upserting job vectors to Qdrant collection=%s count=%s",
        collection_name,
        len(ids),
    )

    upsert_started = time.perf_counter()

    try:
        raw_point_ids = store.upsert(
            collection_name,
            ids=ids,
            vectors=vectors,
            payloads=payloads,
        )
    except Exception:
        logger.exception(
            "Failed while upserting job vectors to Qdrant collection=%s count=%s",
            collection_name,
            len(ids),
        )
        raise

    point_ids = _fallback_point_ids(ids, raw_point_ids)

    upsert_elapsed = time.perf_counter() - upsert_started

    logger.info(
        "Qdrant job upsert completed collection=%s point_count=%s elapsed_seconds=%.3f",
        collection_name,
        len(point_ids),
        upsert_elapsed,
    )

    _mark_indexed_records(
        repo=repo,
        rows=rows,
        point_ids=point_ids,
        record_type="job",
        collection_name=collection_name,
        embedding_model=embedder.model,
        vector_size=vector_size,
    )

    summary = {
        "record_type": "job",
        "collection_name": collection_name,
        "input_records": len(rows_from_mongo),
        "indexable_records": len(rows),
        "indexed_records": len(point_ids),
        "skipped_empty_text": skipped_empty_text,
        "skipped_missing_id": skipped_missing_id,
        "vector_size": vector_size,
        "embedding_model": embedder.model,
        "recreate": recreate,
        "only_pending": only_pending,
        "limit": limit,
        "embedding_elapsed_seconds": round(embedding_elapsed, 3),
        "qdrant_upsert_elapsed_seconds": round(upsert_elapsed, 3),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "status": "completed",
    }

    logger.info("Finished job tower indexing summary=%s", summary)

    return summary


def index_candidate_towers(
    *,
    repo: WarehouseRepository,
    store: QdrantVectorStore,
    embedder: OllamaEmbedder,
    collection_name: str,
    recreate: bool = False,
    only_pending: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()

    logger.info(
        "Starting candidate tower indexing collection=%s recreate=%s only_pending=%s limit=%s",
        collection_name,
        recreate,
        only_pending,
        limit,
    )

    rows_from_mongo = repo.candidate_towers(
        only_pending=only_pending,
        limit=limit,
    )

    logger.info(
        "Fetched candidate tower rows from MongoDB count=%s",
        len(rows_from_mongo),
    )

    rows: list[dict[str, Any]] = []
    skipped_empty_text = 0
    skipped_missing_id = 0

    for row in rows_from_mongo:
        candidate_id = str(row.get("candidate_id") or "").strip()
        text = str(row.get("candidate_embedding_text") or "").strip()

        if not candidate_id:
            skipped_missing_id += 1
            logger.warning(
                "Skipping candidate tower row because candidate_id is missing row_keys=%s",
                sorted(row.keys()),
            )
            continue

        if not text:
            skipped_empty_text += 1
            logger.warning(
                "Skipping candidate_id=%s because candidate_embedding_text is empty",
                candidate_id,
            )
            continue

        rows.append(row)

    logger.info(
        "Prepared indexable candidate rows input=%s indexable=%s skipped_empty_text=%s skipped_missing_id=%s",
        len(rows_from_mongo),
        len(rows),
        skipped_empty_text,
        skipped_missing_id,
    )

    if not rows:
        summary = {
            "record_type": "candidate",
            "collection_name": collection_name,
            "input_records": len(rows_from_mongo),
            "indexable_records": 0,
            "indexed_records": 0,
            "skipped_empty_text": skipped_empty_text,
            "skipped_missing_id": skipped_missing_id,
            "embedding_model": embedder.model,
            "status": "no_indexable_records",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

        logger.warning("No indexable candidate tower records found. Summary=%s", summary)
        return summary

    ids = [
        f"candidate:{row['candidate_id']}:{embedder.model}"
        for row in rows
    ]

    texts = [
        str(row["candidate_embedding_text"])
        for row in rows
    ]

    payloads = [
        _candidate_payload(row, embedding_model=embedder.model)
        for row in rows
    ]

    logger.info(
        "Candidate embedding text stats=%s",
        _text_length_stats(texts),
    )

    logger.info(
        "Generating candidate embeddings model=%s text_count=%s",
        embedder.model,
        len(texts),
    )

    embedding_started = time.perf_counter()

    try:
        vectors = embedder.embed_many(texts)
    except Exception:
        logger.exception(
            "Failed while generating candidate embeddings model=%s text_count=%s",
            embedder.model,
            len(texts),
        )
        raise

    embedding_elapsed = time.perf_counter() - embedding_started
    vector_size = _validate_vectors(vectors, len(texts))

    logger.info(
        "Generated candidate embeddings count=%s vector_size=%s elapsed_seconds=%.3f",
        len(vectors),
        vector_size,
        embedding_elapsed,
    )

    logger.info(
        "Ensuring Qdrant candidate collection collection=%s vector_size=%s recreate=%s",
        collection_name,
        vector_size,
        recreate,
    )

    store.ensure_collection(
        collection_name,
        vector_size=vector_size,
        recreate=recreate,
    )

    logger.info(
        "Upserting candidate vectors to Qdrant collection=%s count=%s",
        collection_name,
        len(ids),
    )

    upsert_started = time.perf_counter()

    try:
        raw_point_ids = store.upsert(
            collection_name,
            ids=ids,
            vectors=vectors,
            payloads=payloads,
        )
    except Exception:
        logger.exception(
            "Failed while upserting candidate vectors to Qdrant collection=%s count=%s",
            collection_name,
            len(ids),
        )
        raise

    point_ids = _fallback_point_ids(ids, raw_point_ids)

    upsert_elapsed = time.perf_counter() - upsert_started

    logger.info(
        "Qdrant candidate upsert completed collection=%s point_count=%s elapsed_seconds=%.3f",
        collection_name,
        len(point_ids),
        upsert_elapsed,
    )

    _mark_indexed_records(
        repo=repo,
        rows=rows,
        point_ids=point_ids,
        record_type="candidate",
        collection_name=collection_name,
        embedding_model=embedder.model,
        vector_size=vector_size,
    )

    summary = {
        "record_type": "candidate",
        "collection_name": collection_name,
        "input_records": len(rows_from_mongo),
        "indexable_records": len(rows),
        "indexed_records": len(point_ids),
        "skipped_empty_text": skipped_empty_text,
        "skipped_missing_id": skipped_missing_id,
        "vector_size": vector_size,
        "embedding_model": embedder.model,
        "recreate": recreate,
        "only_pending": only_pending,
        "limit": limit,
        "embedding_elapsed_seconds": round(embedding_elapsed, 3),
        "qdrant_upsert_elapsed_seconds": round(upsert_elapsed, 3),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "status": "completed",
    }

    logger.info("Finished candidate tower indexing summary=%s", summary)

    return summary