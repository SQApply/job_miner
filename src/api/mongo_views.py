from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from ..common.constants import MongoCollections
from ..infrastructure.mongo import get_mongo_database


def _clean_doc(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out


def _mongo_safe(value: Any) -> Any:
    """Return a Mongo/BSON-safe copy of a value.

    Postgres rows returned by psycopg may contain native uuid.UUID values.
    PyMongo's default UuidRepresentation.UNSPECIFIED rejects native UUIDs,
    so app/user identifiers must be persisted in Mongo as strings unless the
    Mongo client is explicitly configured for UUID binary representation.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _mongo_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mongo_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_mongo_safe(item) for item in value]
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(email: str | None) -> str | None:
    value = str(email or "").strip().lower()
    return value or None


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def get_candidate_profile(candidate_id: str) -> dict[str, Any] | None:
    db = get_mongo_database()
    tower = _clean_doc(db[MongoCollections.CANDIDATE_TOWER_RECORDS].find_one({"candidate_id": candidate_id}))
    if not tower:
        return None
    resume_id = tower.get("resume_id")
    resume = _clean_doc(db[MongoCollections.RESUME_PROFILES_CURRENT].find_one({"resume_id": resume_id})) if resume_id else None
    return {"candidate_tower": tower, "resume_profile": resume}


def candidate_exists(candidate_id: str) -> tuple[bool, str | None]:
    db = get_mongo_database()
    doc = db[MongoCollections.CANDIDATE_TOWER_RECORDS].find_one({"candidate_id": candidate_id}, {"resume_id": 1})
    if not doc:
        return False, None
    return True, doc.get("resume_id")


def find_candidates_by_verified_email(email: str) -> list[dict[str, Any]]:
    """Find candidate tower records that match a verified login email.

    The matching is case-insensitive and checks both the flattened tower email
    and the canonical resume profile contact email. The caller must only use
    this for verified emails from Keycloak to avoid account takeover.
    """
    normalized = _normalize_email(email)
    if not normalized:
        return []

    db = get_mongo_database()
    escaped = re.escape(normalized)
    email_regex = re.compile(f"^{escaped}$", re.IGNORECASE)

    candidates: dict[str, dict[str, Any]] = {}
    for tower in db[MongoCollections.CANDIDATE_TOWER_RECORDS].find({"email": email_regex}).limit(10):
        clean = _clean_doc(tower) or {}
        candidate_id = clean.get("candidate_id")
        if candidate_id:
            candidates[str(candidate_id)] = clean

    resume_ids = [
        row.get("resume_id")
        for row in db[MongoCollections.RESUME_PROFILES_CURRENT]
        .find({"contact.email": email_regex}, {"resume_id": 1})
        .limit(10)
        if row.get("resume_id")
    ]
    if resume_ids:
        for tower in db[MongoCollections.CANDIDATE_TOWER_RECORDS].find({"resume_id": {"$in": resume_ids}}).limit(10):
            clean = _clean_doc(tower) or {}
            candidate_id = clean.get("candidate_id")
            if candidate_id:
                candidates[str(candidate_id)] = clean

    return list(candidates.values())


def create_incomplete_candidate_profile_for_user(user: dict[str, Any]) -> dict[str, Any]:
    """Create an onboarding candidate shell for a verified first-time user.

    This gives every fresh Google/Keycloak candidate a safe profile immediately,
    without exposing manual candidate_id linking. Resume parsing and embeddings
    can later replace this incomplete shell with a full profile.
    """
    email = _normalize_email(user.get("email"))
    if not email:
        raise ValueError("Cannot create candidate profile shell without email.")

    full_name = user.get("full_name") or user.get("preferred_username") or email.split("@")[0]
    digest = _stable_hash(f"candidate-shell:{email}")
    candidate_id = f"candidate_{digest[:24]}"
    resume_id = f"profile_{digest[:24]}"
    now = _utc_now()
    content_hash = _stable_hash({"email": email, "candidate_id": candidate_id, "profile_state": "incomplete"})

    db = get_mongo_database()
    resume_doc = {
        "resume_id": resume_id,
        "sha256": digest,
        "source_file_name": "keycloak_onboarding_profile",
        "contact": {
            "full_name": full_name,
            "email": email,
            "phone": None,
            "location": None,
        },
        "headline": None,
        "summary": None,
        "total_experience_years": None,
        "current_title": None,
        "current_company": None,
        "primary_skills": [],
        "secondary_skills": [],
        "tools_and_platforms": [],
        "programming_languages": [],
        "domains": [],
        "certifications": [],
        "experience": [],
        "education": [],
        "projects": [],
        "languages": [],
        "parse_warnings": ["Profile shell created during first login. Resume upload is required."],
        "content_hash": content_hash,
        "first_seen_at": now,
        "last_seen_at": now,
        "last_run_session_id": None,
        "is_active": True,
        "profile_state": "incomplete",
        "onboarding_required": True,
        "raw_payload": {"created_from": "keycloak_login", "app_user_id": user.get("id")},
    }
    tower_doc = {
        "candidate_id": candidate_id,
        "resume_id": resume_id,
        "source_file_name": "keycloak_onboarding_profile",
        "sha256": digest,
        "full_name": full_name,
        "email": email,
        "phone": None,
        "location": None,
        "current_title": None,
        "current_company": None,
        "total_experience_years": None,
        "skills": [],
        "primary_skills": [],
        "secondary_skills": [],
        "domains": [],
        "identity_text": f"{full_name} {email}".strip(),
        "skills_text": "",
        "experience_text": "",
        "education_text": "",
        "candidate_embedding_text": f"{full_name} {email}".strip(),
        "source_content_hash": content_hash,
        "embedding_status": "pending",
        "embedding_model": None,
        "last_indexed_at": None,
        "profile_state": "incomplete",
        "onboarding_required": True,
        "created_from": "keycloak_login",
        "created_at": now,
        "updated_at": now,
    }

    resume_doc = _mongo_safe(resume_doc)
    tower_doc = _mongo_safe(tower_doc)

    # MongoDB does not allow the same field to be present in both $setOnInsert
    # and $set for a single upsert. Keep creation timestamps in $setOnInsert and
    # mutable "last seen / updated" timestamps in $set only.
    resume_insert_doc = dict(resume_doc)
    resume_insert_doc.pop("last_seen_at", None)

    tower_insert_doc = dict(tower_doc)
    tower_insert_doc.pop("updated_at", None)

    db[MongoCollections.RESUME_PROFILES_CURRENT].update_one(
        {"resume_id": resume_id},
        {"$setOnInsert": resume_insert_doc, "$set": {"last_seen_at": now}},
        upsert=True,
    )
    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
        {"candidate_id": candidate_id},
        {"$setOnInsert": tower_insert_doc, "$set": {"updated_at": now}},
        upsert=True,
    )
    return tower_doc


def list_candidate_recommendations(candidate_id: str, source: str = "llm", limit: int = 50) -> list[dict[str, Any]]:
    db = get_mongo_database()
    requested_source = source

    if source == "baseline":
        collection = MongoCollections.CANDIDATE_JOB_MATCHES
        sort_key = "rank"
        query: dict[str, Any] = {"candidate_id": candidate_id}
    else:
        collection = MongoCollections.CANDIDATE_JOB_MATCHES_LLM_RERANKED
        sort_key = "final_rank"
        query = {
            "candidate_id": candidate_id,
            # Candidate-facing LLM recommendations should include only records that were
            # actually scored by the LLM. Old fallback records are hidden even if they are
            # still present from previous runs.
            "evidence.reranker_status": "llm_scored",
        }

    rows = list(
        db[collection]
        .find(query)
        .sort([("match_run_id", -1), (sort_key, 1)])
        .limit(limit)
    )

    if requested_source != "baseline" and not rows and os.getenv("JOB_MINER_RECOMMENDATIONS_FALLBACK_TO_BASELINE", "true").strip().lower() in {"1", "true", "yes", "on"}:
        collection = MongoCollections.CANDIDATE_JOB_MATCHES
        sort_key = "rank"
        rows = list(
            db[collection]
            .find({"candidate_id": candidate_id})
            .sort([("match_run_id", -1), (sort_key, 1)])
            .limit(limit)
        )

    out: list[dict[str, Any]] = []
    for row in rows:
        row = _clean_doc(row) or {}
        job_id = row.get("job_id")
        job = _clean_doc(db[MongoCollections.JOBS_CURRENT].find_one({"job_id": job_id})) if job_id else None
        if job:
            row["job"] = job
        row["source_collection"] = collection
        out.append(row)
    return out


def _job_catalog_projection() -> dict[str, int]:
    return {
        "_id": 0,
        "job_id": 1,
        "title": 1,
        "job_title": 1,
        "company": 1,
        "company_name": 1,
        "employer": 1,
        "location": 1,
        "location_text": 1,
        "job_url": 1,
        "apply_url": 1,
        "source_url": 1,
        "url": 1,
        "description": 1,
        "summary": 1,
        "required_skills": 1,
        "preferred_skills": 1,
        "skills": 1,
        "employment_type": 1,
        "posted_at": 1,
        "created_at": 1,
        "updated_at": 1,
        "catalog_visible": 1,
        "validation_status": 1,
    }
    
def _candidate_visible_job_filter() -> dict[str, Any]:
    """Return Mongo filter for jobs that are safe to show in candidate-facing views.

    Raw scraped jobs can be incomplete. Candidate-facing APIs should not show
    placeholder records such as "Untitled job".
    """
    return {
        "$and": [
            {"catalog_visible": {"$ne": False}},
            {"validation_status": {"$nin": ["invalid", "invalid_missing_title", "invalid_missing_company", "invalid_missing_url"]}},
            {
                "$or": [
                    {"title": {"$type": "string", "$regex": r"\S"}},
                    {"job_title": {"$type": "string", "$regex": r"\S"}},
                ]
            },
            {
                "$or": [
                    {"company": {"$type": "string", "$regex": r"\S"}},
                    {"company_name": {"$type": "string", "$regex": r"\S"}},
                    {"employer": {"$type": "string", "$regex": r"\S"}},
                ]
            },
            {
                "$or": [
                    {"job_url": {"$type": "string", "$regex": r"\S"}},
                    {"apply_url": {"$type": "string", "$regex": r"\S"}},
                    {"source_url": {"$type": "string", "$regex": r"\S"}},
                    {"url": {"$type": "string", "$regex": r"\S"}},
                ]
            },
            {
                "$nor": [
                    {"title": {"$regex": r"^\s*untitled\s+job\s*$", "$options": "i"}},
                    {"job_title": {"$regex": r"^\s*untitled\s+job\s*$", "$options": "i"}},
                    {"title": {"$regex": r"^\s*(n/a|na|none|null|unknown)\s*$", "$options": "i"}},
                    {"job_title": {"$regex": r"^\s*(n/a|na|none|null|unknown)\s*$", "$options": "i"}},
                ]
            },
        ]
    }

def _job_catalog_search_query(search_text: str | None) -> dict[str, Any]:
    value = str(search_text or "").strip()
    if not value:
        return {}

    regex = re.compile(re.escape(value), re.IGNORECASE)
    return {
        "$or": [
            {"title": regex},
            {"job_title": regex},
            {"company": regex},
            {"company_name": regex},
            {"employer": regex},
            {"location": regex},
            {"location_text": regex},
            {"description": regex},
            {"summary": regex},
            {"required_skills": regex},
            {"preferred_skills": regex},
            {"skills": regex},
        ]
    }

def list_all_jobs_catalog(*, limit: int = 50, offset: int = 0, q: str | None = None) -> dict[str, Any]:
    """Return the candidate-facing full jobs catalog.

    The catalog is paginated and sorted alphabetically so the frontend does not
    load thousands of job records into the browser at once. This is intentionally
    read-only; save/apply actions continue to use the existing job action APIs.
    """
    db = get_mongo_database()
    safe_limit = min(max(int(limit or 50), 1), 100)
    safe_offset = max(int(offset or 0), 0)
    # query = _job_catalog_search_query(q)
    base_filter = _candidate_visible_job_filter()
    search_filter = _job_catalog_search_query(q)
    query = {"$and": [base_filter, search_filter]} if search_filter else base_filter

    total = db[MongoCollections.JOBS_CURRENT].count_documents(query)
    rows = list(
        db[MongoCollections.JOBS_CURRENT]
        .find(query, _job_catalog_projection())
        .sort([("title", 1), ("company", 1), ("job_id", 1)])
        .skip(safe_offset)
        .limit(safe_limit)
    )

    jobs: list[dict[str, Any]] = []
    for row in rows:
        job = _clean_doc(row) or {}
        job["source_collection"] = MongoCollections.JOBS_CURRENT
        job["apply_url"] = job.get("apply_url") or job.get("job_url") or job.get("source_url") or job.get("url")
        jobs.append(job)

    next_offset = safe_offset + safe_limit if safe_offset + safe_limit < total else None
    previous_offset = max(safe_offset - safe_limit, 0) if safe_offset > 0 else None

    return {
        "jobs": jobs,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "next_offset": next_offset,
        "previous_offset": previous_offset,
        "q": str(q or "").strip(),
    }


def get_job_by_id(job_id: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    db = get_mongo_database()
    return _clean_doc(db[MongoCollections.JOBS_CURRENT].find_one({"job_id": job_id}))


def get_recommended_job(candidate_id: str, job_id: str, source: str = "llm") -> dict[str, Any] | None:
    db = get_mongo_database()
    collection = MongoCollections.CANDIDATE_JOB_MATCHES_LLM_RERANKED if source != "baseline" else MongoCollections.CANDIDATE_JOB_MATCHES
    row = _clean_doc(db[collection].find_one({"candidate_id": candidate_id, "job_id": job_id}))
    job = _clean_doc(db[MongoCollections.JOBS_CURRENT].find_one({"job_id": job_id}))
    if not row and not job:
        return None
    return {"match": row, "job": job, "source_collection": collection}


def warehouse_counts() -> dict[str, int]:
    db = get_mongo_database()
    names = [
        MongoCollections.JOBS_CURRENT,
        MongoCollections.RESUME_PROFILES_CURRENT,
        MongoCollections.CANDIDATE_TOWER_RECORDS,
        MongoCollections.JOB_TOWER_RECORDS,
        MongoCollections.CANDIDATE_JOB_MATCHES,
        MongoCollections.CANDIDATE_JOB_MATCHES_LLM_RERANKED,
        MongoCollections.CANDIDATE_JOB_FEEDBACK,
        "qdrant_index_state",
    ]
    return {name: db[name].count_documents({}) for name in names}
