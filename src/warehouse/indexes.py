from __future__ import annotations

from pymongo import ASCENDING, IndexModel
from pymongo.database import Database

INDEXES: dict[str, list[IndexModel]] = {
    "warehouse_run_sessions": [IndexModel([("run_session_id", ASCENDING)], unique=True)],
    "job_raw_extractions": [IndexModel([("raw_id", ASCENDING)], unique=True), IndexModel([("target_id", ASCENDING), ("source_url", ASCENDING)])],
    "jobs_current": [IndexModel([("job_id", ASCENDING)], unique=True), IndexModel([("target_id", ASCENDING), ("job_url", ASCENDING)], unique=True, sparse=True), IndexModel([("is_active", ASCENDING)]), IndexModel([("last_seen_at", ASCENDING)])],
    "jobs_history": [IndexModel([("history_id", ASCENDING)], unique=True), IndexModel([("job_id", ASCENDING), ("content_hash", ASCENDING)], unique=True)],
    "resume_profiles_current": [IndexModel([("resume_id", ASCENDING)], unique=True), IndexModel([("sha256", ASCENDING)], unique=True), IndexModel([("contact.email", ASCENDING)], sparse=True)],
    "resume_profiles_history": [IndexModel([("history_id", ASCENDING)], unique=True), IndexModel([("resume_id", ASCENDING), ("content_hash", ASCENDING)], unique=True)],
    "job_tower_records": [IndexModel([("job_tower_id", ASCENDING)], unique=True), IndexModel([("job_id", ASCENDING)], unique=True), IndexModel([("embedding_status", ASCENDING)])],
    "candidate_tower_records": [IndexModel([("candidate_id", ASCENDING)], unique=True), IndexModel([("resume_id", ASCENDING)], unique=True), IndexModel([("embedding_status", ASCENDING)])],
    "qdrant_index_state": [IndexModel([("index_id", ASCENDING)], unique=True), IndexModel([("record_type", ASCENDING), ("record_id", ASCENDING), ("embedding_model", ASCENDING)], unique=True)],
    "candidate_job_matches": [IndexModel([("match_run_id", ASCENDING)]), IndexModel([("candidate_id", ASCENDING), ("rank", ASCENDING)]), IndexModel([("candidate_id", ASCENDING), ("job_id", ASCENDING), ("match_run_id", ASCENDING)], unique=True)],
}


def init_indexes(db: Database) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, indexes in INDEXES.items():
        db[name].create_indexes(indexes)
        out[name] = len(indexes)
    return out
