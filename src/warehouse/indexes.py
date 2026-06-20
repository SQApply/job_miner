from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.database import Database

# -----------------------------------------------------------------------------
# MongoDB Index Summary
# -----------------------------------------------------------------------------
# Collection: warehouse_run_sessions
# Index: run_session_id
# Type: Unique
# Why: Ensures one warehouse run/session record is stored only once.

# Collection: job_raw_extractions
# Index: raw_id
# Type: Unique
# Why: Prevents duplicate raw job extraction payloads.

# Collection: job_raw_extractions
# Index: target_id + source_url
# Type: Normal compound index
# Why: Quickly finds raw extracted jobs for a specific website/source URL.

# Collection: jobs_current
# Index: job_id
# Type: Unique
# Why: Ensures one current job document exists per deterministic job ID.

# Collection: jobs_current
# Index: target_id + job_url
# Type: Unique sparse compound index
# Why: Prevents duplicate job URLs within the same target; sparse allows records where job_url is missing.

# Collection: jobs_current
# Index: is_active
# Type: Normal index
# Why: Quickly fetches active/current jobs for job tower generation.

# Collection: jobs_current
# Index: last_seen_at
# Type: Normal index
# Why: Quickly finds recently seen or stale jobs during incremental runs.

# Collection: jobs_current
# Index: posted_at / first_seen_at
# Type: Normal descending indexes
# Why: Supports the candidate catalog freshness sort and its 1 day, 3 days,
#      1/2/3 week, 1 month, and older-than-one-month filters.

# Collection: jobs_history
# Index: history_id
# Type: Unique
# Why: Ensures each historical job version document is stored only once.

# Collection: jobs_history
# Index: job_id + content_hash
# Type: Unique compound index
# Why: Avoids duplicate history versions for the same job content.

# Collection: resume_profiles_current
# Index: resume_id
# Type: Unique
# Why: Ensures one current parsed resume profile exists per resume ID.

# Collection: resume_profiles_current
# Index: sha256
# Type: Unique
# Why: Deduplicates the same resume file based on file hash.

# Collection: resume_profiles_current
# Index: contact.email
# Type: Sparse normal index
# Why: Allows lookup of candidates by email; sparse allows resumes where email is missing.

# Collection: resume_profiles_history
# Index: history_id
# Type: Unique
# Why: Ensures each historical resume profile version is stored only once.

# Collection: resume_profiles_history
# Index: resume_id + content_hash
# Type: Unique compound index
# Why: Avoids duplicate resume history versions for the same parsed resume content.

# Collection: job_tower_records
# Index: job_tower_id
# Type: Unique
# Why: Ensures one unique job tower record document ID.

# Collection: job_tower_records
# Index: job_id
# Type: Unique
# Why: Ensures one current embedding-ready tower record exists per job.

# Collection: job_tower_records
# Index: embedding_status
# Type: Normal index
# Why: Quickly finds job tower records that are pending Qdrant indexing.

# Collection: candidate_tower_records
# Index: candidate_id
# Type: Unique
# Why: Ensures one unique candidate tower record per candidate.

# Collection: candidate_tower_records
# Index: resume_id
# Type: Unique
# Why: Ensures one candidate tower record is created per parsed resume.

# Collection: candidate_tower_records
# Index: embedding_status
# Type: Normal index
# Why: Quickly finds candidate tower records that are pending Qdrant indexing.

# Collection: qdrant_index_state
# Index: index_id
# Type: Unique
# Why: Ensures one Qdrant indexing state record per indexed item.

# Collection: qdrant_index_state
# Index: record_type + record_id + embedding_model
# Type: Unique compound index
# Why: Tracks one vector indexing state per job/candidate record and embedding model.

# Collection: candidate_job_matches
# Index: match_run_id
# Type: Normal index
# Why: Quickly fetches all matches produced by a specific matching run.

# Collection: candidate_job_matches
# Index: candidate_id + rank
# Type: Normal compound index
# Why: Quickly fetches ranked job recommendations for a candidate.

# Collection: candidate_job_matches
# Index: candidate_id + job_id + match_run_id
# Type: Unique compound index
# Why: Prevents duplicate candidate-job match records inside the same matching run.
# -----------------------------------------------------------------------------

INDEXES: dict[str, list[IndexModel]] = {
    "warehouse_run_sessions": [IndexModel([("run_session_id", ASCENDING)], unique=True)],
    "job_raw_extractions": [IndexModel([("raw_id", ASCENDING)], unique=True), IndexModel([("target_id", ASCENDING), ("source_url", ASCENDING)])],
    "jobs_current": [
        IndexModel([("job_id", ASCENDING)], unique=True),
        IndexModel([("target_id", ASCENDING), ("job_url", ASCENDING)], unique=True, sparse=True),
        IndexModel([("is_active", ASCENDING)]),
        IndexModel([("last_seen_at", DESCENDING)]),
        IndexModel([("posted_at", DESCENDING)]),
        IndexModel([("first_seen_at", DESCENDING)]),
    ],
    "jobs_history": [IndexModel([("history_id", ASCENDING)], unique=True), IndexModel([("job_id", ASCENDING), ("content_hash", ASCENDING)], unique=True)],
    "resume_profiles_current": [IndexModel([("resume_id", ASCENDING)], unique=True), IndexModel([("sha256", ASCENDING)], unique=True), IndexModel([("contact.email", ASCENDING)], sparse=True)],
    "resume_profiles_history": [IndexModel([("history_id", ASCENDING)], unique=True), IndexModel([("resume_id", ASCENDING), ("content_hash", ASCENDING)], unique=True)],
    "job_tower_records": [IndexModel([("job_tower_id", ASCENDING)], unique=True), IndexModel([("job_id", ASCENDING)], unique=True), IndexModel([("embedding_status", ASCENDING)])],
    "candidate_tower_records": [IndexModel([("candidate_id", ASCENDING)], unique=True), IndexModel([("resume_id", ASCENDING)], unique=True), IndexModel([("embedding_status", ASCENDING)])],
    "qdrant_index_state": [IndexModel([("index_id", ASCENDING)], unique=True), IndexModel([("record_type", ASCENDING), ("record_id", ASCENDING), ("embedding_model", ASCENDING)], unique=True)],
    "candidate_job_matches": [IndexModel([("match_run_id", ASCENDING)]), IndexModel([("candidate_id", ASCENDING), ("rank", ASCENDING)]), IndexModel([("candidate_id", ASCENDING), ("job_id", ASCENDING), ("match_run_id", ASCENDING)], unique=True)],
    "candidate_job_matches_llm_reranked": [
        IndexModel([("match_run_id", ASCENDING)]),
        IndexModel([("candidate_id", ASCENDING), ("match_run_id", ASCENDING), ("final_rank", ASCENDING)]),
        IndexModel([("candidate_id", ASCENDING), ("job_id", ASCENDING), ("match_run_id", ASCENDING)], unique=True),
        IndexModel([("candidate_id", ASCENDING), ("evidence.reranker_status", ASCENDING), ("match_run_id", ASCENDING), ("final_rank", ASCENDING)]),
    ],
    "candidate_profile_edit_events": [
        IndexModel([("candidate_id", ASCENDING), ("created_at", ASCENDING)]),
        IndexModel([("edit_id", ASCENDING)], unique=True),
    ],
}


def init_indexes(db: Database) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, indexes in INDEXES.items():
        db[name].create_indexes(indexes)
        out[name] = len(indexes)
    return out
