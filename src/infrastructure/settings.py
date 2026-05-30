from __future__ import annotations

import os
from dataclasses import dataclass

from ..common.env import load_runtime_env

load_runtime_env()

def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True, slots=True)
class WarehouseSettings:
    mongo_uri: str = "mongodb://job_miner:job_miner@localhost:27017/?authSource=admin"
    mongo_db: str = "job_miner"


@dataclass(frozen=True, slots=True)
class VectorSettings:
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    jobs_collection: str = "job_miner_jobs"
    candidates_collection: str = "job_miner_candidates"
    ollama_url: str = "http://localhost:11434"
    embedding_model: str = "embeddinggemma"
    embedding_batch_size: int = 16
    max_chars_per_text: int = 12000


@dataclass(frozen=True, slots=True)
class AppSettings:
    warehouse: WarehouseSettings
    vector: VectorSettings


def load_app_settings() -> AppSettings:
    return AppSettings(
        warehouse=WarehouseSettings(
            mongo_uri=os.getenv("JOB_MINER_MONGO_URI", "mongodb://job_miner:job_miner@localhost:27017/?authSource=admin"),
            mongo_db=os.getenv("JOB_MINER_MONGO_DB", "job_miner"),
        ),
        vector=VectorSettings(
            qdrant_url=os.getenv("JOB_MINER_QDRANT_URL", "http://localhost:6333"),
            qdrant_api_key=os.getenv("JOB_MINER_QDRANT_API_KEY") or None,
            jobs_collection=os.getenv("JOB_MINER_JOBS_COLLECTION", "job_miner_jobs"),
            candidates_collection=os.getenv("JOB_MINER_CANDIDATES_COLLECTION", "job_miner_candidates"),
            ollama_url=os.getenv("JOB_MINER_OLLAMA_URL", "http://localhost:11434"),
            embedding_model=os.getenv("JOB_MINER_EMBEDDING_MODEL", "embeddinggemma"),
            embedding_batch_size=_int_env("JOB_MINER_EMBEDDING_BATCH_SIZE", 16),
            max_chars_per_text=_int_env("JOB_MINER_MAX_CHARS_PER_TEXT", 12000),
        ),
    )
