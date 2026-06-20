from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
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

    cleaned_rows = [_clean_doc(row) or {} for row in rows]
    job_ids = sorted({str(row.get("job_id")) for row in cleaned_rows if row.get("job_id")})
    jobs_by_id: dict[str, dict[str, Any]] = {}

    if job_ids:
        job_query = {"$and": [_candidate_visible_job_filter(), {"job_id": {"$in": job_ids}}]}
        for job_doc in db[MongoCollections.JOBS_CURRENT].find(job_query, _job_catalog_projection()):
            clean_job = _clean_doc(job_doc) or {}
            job_id = str(clean_job.get("job_id") or "")
            if job_id:
                jobs_by_id[job_id] = _job_snapshot(clean_job)

    out: list[dict[str, Any]] = []
    for row in cleaned_rows:
        job_id = str(row.get("job_id") or "")
        job = jobs_by_id.get(job_id)

        if not job and isinstance(row.get("job_snapshot"), dict):
            snapshot = _job_snapshot(row["job_snapshot"])
            if _is_candidate_visible_job(snapshot):
                job = snapshot

        # Defensive guard: recommendation rows can outlive source jobs or point
        # to raw/invalid jobs. Do not leak bad records into candidate-facing UI.
        if not job or not _is_candidate_visible_job(job):
            continue

        row["job"] = job
        row.setdefault("job_snapshot", job)
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
        "posted_date": 1,
        "posted_at": 1,
        "first_seen_at": 1,
        "last_seen_at": 1,
        "created_at": 1,
        "updated_at": 1,
        "catalog_visible": 1,
        "validation_status": 1,
    }


CATALOG_FRESHNESS_OPTIONS: tuple[dict[str, Any], ...] = (
    {"value": "1d", "label": "Last 1 day", "days": 1},
    {"value": "3d", "label": "Last 3 days", "days": 3},
    {"value": "1w", "label": "Last 1 week", "days": 7},
    {"value": "2w", "label": "Last 2 weeks", "days": 14},
    {"value": "3w", "label": "Last 3 weeks", "days": 21},
    {"value": "1mo", "label": "Last 1 month", "days": 30},
    {"value": "older_than_1mo", "label": "More than 1 month", "days": None},
)

CATALOG_SORTS = {"newest", "oldest", "title_asc", "title_desc"}
CATALOG_WORK_MODE_LABELS = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "on_site": "On-site",
}
_MAX_CATALOG_FILTER_VALUES = 12
_MAX_CATALOG_FACET_VALUES = 8
_REMOTE_WORK_MODE_PATTERN = r"\b(remote|work[\s-]?from[\s-]?home|wfh|anywhere)\b"
_HYBRID_WORK_MODE_PATTERN = r"\bhybrid\b"


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, normalized job object safe for candidate-facing UI."""
    title = str(job.get("title") or job.get("job_title") or "").strip()
    company = str(job.get("company") or job.get("company_name") or job.get("employer") or "").strip()
    location_text = str(job.get("location_text") or job.get("location") or "").strip()
    apply_url = job.get("apply_url") or job.get("job_url") or job.get("source_url") or job.get("url")
    freshness_at = job.get("_catalog_freshness_at") or job.get("posted_at") or job.get("first_seen_at") or job.get("created_at")

    return {
        "job_id": job.get("job_id"),
        "title": title,
        "company": company,
        "location_text": location_text,
        "apply_url": apply_url,
        "job_url": job.get("job_url"),
        "source_url": job.get("source_url"),
        "url": job.get("url"),
        "description": job.get("description"),
        "summary": job.get("summary"),
        "required_skills": job.get("required_skills") or [],
        "preferred_skills": job.get("preferred_skills") or [],
        "skills": job.get("skills") or [],
        "employment_type": job.get("employment_type"),
        "posted_date": job.get("posted_date"),
        "posted_at": job.get("posted_at"),
        "first_seen_at": job.get("first_seen_at"),
        "freshness_at": freshness_at,
        "freshness_source": "source_posted_date" if job.get("posted_at") else "first_seen_at",
    }


def _is_candidate_visible_job(job: dict[str, Any] | None) -> bool:
    if not job:
        return False

    title = str(job.get("title") or job.get("job_title") or "").strip()
    company = str(job.get("company") or job.get("company_name") or job.get("employer") or "").strip()
    apply_url = job.get("apply_url") or job.get("job_url") or job.get("source_url") or job.get("url")
    validation_status = str(job.get("validation_status") or "").strip().lower()

    if job.get("catalog_visible") is False:
        return False
    if validation_status in {"invalid", "invalid_missing_title", "invalid_missing_company", "invalid_missing_url"}:
        return False
    if not title or title.lower() in {"untitled job", "n/a", "na", "none", "null", "unknown"}:
        return False
    if not company:
        return False
    if not str(apply_url or "").strip():
        return False
    return True


def _candidate_visible_job_filter() -> dict[str, Any]:
    """Return a Mongo filter for jobs safe to show in candidate-facing views."""
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


def _catalog_timestamp_expression() -> dict[str, Any]:
    """Source posting timestamp, with warehouse first-seen timestamp as fallback."""
    return {
        "$ifNull": [
            "$posted_at",
            {"$ifNull": ["$first_seen_at", "$created_at"]},
        ]
    }


def _normalise_filter_values(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in values or []:
        value = " ".join(str(raw or "").split()).strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) >= _MAX_CATALOG_FILTER_VALUES:
            break
    return cleaned


def _normalise_work_modes(values: list[str] | tuple[str, ...] | None) -> list[str]:
    aliases = {
        "remote": "remote",
        "hybrid": "hybrid",
        "on_site": "on_site",
        "onsite": "on_site",
        "on-site": "on_site",
        "on site": "on_site",
    }
    modes: list[str] = []
    for value in _normalise_filter_values(values):
        normalised = aliases.get(value.casefold())
        if normalised and normalised not in modes:
            modes.append(normalised)
    return modes


def _normalise_freshness(value: str | None) -> str:
    accepted = {option["value"] for option in CATALOG_FRESHNESS_OPTIONS}
    candidate = str(value or "").strip().lower()
    return candidate if candidate in accepted else ""


def _normalise_sort(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in CATALOG_SORTS else "newest"


def _combine_and(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    useful = [condition for condition in conditions if condition]
    if not useful:
        return {}
    if len(useful) == 1:
        return useful[0]
    return {"$and": useful}


def _case_insensitive_exact_filter(field: str, values: list[str]) -> dict[str, Any]:
    if not values:
        return {}
    patterns = [re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE) for value in values]
    return {field: {"$in": patterns}}


def _work_mode_filter(work_modes: list[str]) -> dict[str, Any]:
    if not work_modes:
        return {}

    remote_pattern = re.compile(_REMOTE_WORK_MODE_PATTERN, re.IGNORECASE)
    hybrid_pattern = re.compile(_HYBRID_WORK_MODE_PATTERN, re.IGNORECASE)
    remote_or_hybrid_pattern = re.compile(f"{_REMOTE_WORK_MODE_PATTERN}|{_HYBRID_WORK_MODE_PATTERN}", re.IGNORECASE)
    mode_conditions: list[dict[str, Any]] = []

    if "remote" in work_modes:
        mode_conditions.append({"location_text": remote_pattern})
    if "hybrid" in work_modes:
        mode_conditions.append({"location_text": hybrid_pattern})
    if "on_site" in work_modes:
        mode_conditions.append(
            {
                "$and": [
                    {"location_text": {"$type": "string", "$regex": r"\S"}},
                    {"location_text": {"$not": remote_or_hybrid_pattern}},
                ]
            }
        )

    if not mode_conditions:
        return {}
    if len(mode_conditions) == 1:
        return mode_conditions[0]
    return {"$or": mode_conditions}


def _skills_filter(skills: list[str]) -> dict[str, Any]:
    if not skills:
        return {}
    patterns = [re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE) for value in skills]
    return {
        "$or": [
            {"required_skills": {"$in": patterns}},
            {"preferred_skills": {"$in": patterns}},
            {"skills": {"$in": patterns}},
        ]
    }


def _freshness_filter(freshness: str) -> dict[str, Any]:
    selected = _normalise_freshness(freshness)
    if not selected:
        return {}

    option = next(option for option in CATALOG_FRESHNESS_OPTIONS if option["value"] == selected)
    timestamp = _catalog_timestamp_expression()
    cutoff = _utc_now() - timedelta(days=int(option["days"] or 30))
    comparison = "$lt" if selected == "older_than_1mo" else "$gte"

    return {
        "$expr": {
            "$and": [
                {"$ne": [timestamp, None]},
                {comparison: [timestamp, cutoff]},
            ]
        }
    }


def _build_catalog_query(
    *,
    q: str | None,
    freshness: str | None,
    work_modes: list[str] | tuple[str, ...] | None,
    employment_types: list[str] | tuple[str, ...] | None,
    locations: list[str] | tuple[str, ...] | None,
    companies: list[str] | tuple[str, ...] | None,
    skills: list[str] | tuple[str, ...] | None,
    include_freshness: bool = True,
    include_work_modes: bool = True,
    include_employment_types: bool = True,
    include_locations: bool = True,
    include_companies: bool = True,
    include_skills: bool = True,
) -> dict[str, Any]:
    normalised_work_modes = _normalise_work_modes(work_modes)
    normalised_employment_types = _normalise_filter_values(employment_types)
    normalised_locations = _normalise_filter_values(locations)
    normalised_companies = _normalise_filter_values(companies)
    normalised_skills = _normalise_filter_values(skills)

    conditions = [_candidate_visible_job_filter(), _job_catalog_search_query(q)]
    if include_freshness:
        conditions.append(_freshness_filter(_normalise_freshness(freshness)))
    if include_work_modes:
        conditions.append(_work_mode_filter(normalised_work_modes))
    if include_employment_types:
        conditions.append(_case_insensitive_exact_filter("employment_type", normalised_employment_types))
    if include_locations:
        conditions.append(_case_insensitive_exact_filter("location_text", normalised_locations))
    if include_companies:
        conditions.append(_case_insensitive_exact_filter("company", normalised_companies))
    if include_skills:
        conditions.append(_skills_filter(normalised_skills))
    return _combine_and(conditions)


def _facet_counts_for_field(db: Any, query: dict[str, Any], field: str) -> list[dict[str, Any]]:
    rows = list(
        db[MongoCollections.JOBS_CURRENT].aggregate(
            [
                {"$match": query},
                {"$project": {"value": f"${field}"}},
                {"$match": {"value": {"$type": "string", "$regex": r"\S"}}},
                {"$group": {"_id": "$value", "count": {"$sum": 1}}},
                {"$sort": {"count": -1, "_id": 1}},
                {"$limit": _MAX_CATALOG_FACET_VALUES},
            ]
        )
    )
    return [{"value": str(row["_id"]), "label": str(row["_id"]), "count": int(row["count"])} for row in rows]


def _facet_counts_for_skills(db: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list(
        db[MongoCollections.JOBS_CURRENT].aggregate(
            [
                {"$match": query},
                {
                    "$project": {
                        "values": {
                            "$setUnion": [
                                {"$ifNull": ["$required_skills", []]},
                                {"$ifNull": ["$preferred_skills", []]},
                                {"$ifNull": ["$skills", []]},
                            ]
                        }
                    }
                },
                {"$unwind": "$values"},
                {"$match": {"values": {"$type": "string", "$regex": r"\S"}}},
                {"$group": {"_id": "$values", "count": {"$sum": 1}}},
                {"$sort": {"count": -1, "_id": 1}},
                {"$limit": _MAX_CATALOG_FACET_VALUES},
            ]
        )
    )
    return [{"value": str(row["_id"]), "label": str(row["_id"]), "count": int(row["count"])} for row in rows]


def _facet_counts_for_work_modes(db: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    location_text = {"$convert": {"input": "$location_text", "to": "string", "onError": "", "onNull": ""}}
    rows = list(
        db[MongoCollections.JOBS_CURRENT].aggregate(
            [
                {"$match": query},
                {
                    "$project": {
                        "value": {
                            "$switch": {
                                "branches": [
                                    {
                                        "case": {"$regexMatch": {"input": location_text, "regex": _HYBRID_WORK_MODE_PATTERN, "options": "i"}},
                                        "then": "hybrid",
                                    },
                                    {
                                        "case": {"$regexMatch": {"input": location_text, "regex": _REMOTE_WORK_MODE_PATTERN, "options": "i"}},
                                        "then": "remote",
                                    },
                                    {
                                        "case": {"$regexMatch": {"input": location_text, "regex": r"\S"}},
                                        "then": "on_site",
                                    },
                                ],
                                "default": None,
                            }
                        }
                    }
                },
                {"$match": {"value": {"$in": list(CATALOG_WORK_MODE_LABELS)}}},
                {"$group": {"_id": "$value", "count": {"$sum": 1}}},
            ]
        )
    )
    counts = {str(row["_id"]): int(row["count"]) for row in rows}
    return [
        {"value": value, "label": label, "count": counts.get(value, 0)}
        for value, label in CATALOG_WORK_MODE_LABELS.items()
        if counts.get(value, 0) > 0
    ]


def _freshness_facets(db: Any, base_query_without_freshness: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all timeline counts in one aggregation rather than seven scans."""
    now = _utc_now()
    timestamp = _catalog_timestamp_expression()
    counters: dict[str, Any] = {}
    for option in CATALOG_FRESHNESS_OPTIONS:
        cutoff = now - timedelta(days=int(option["days"] or 30))
        comparison = "$lt" if option["value"] == "older_than_1mo" else "$gte"
        counters[option["value"]] = {
            "$sum": {
                "$cond": [
                    {comparison: ["$freshness_at", cutoff]},
                    1,
                    0,
                ]
            }
        }

    rows = list(
        db[MongoCollections.JOBS_CURRENT].aggregate(
            [
                {"$match": base_query_without_freshness},
                {"$addFields": {"freshness_at": timestamp}},
                {"$match": {"$expr": {"$ne": ["$freshness_at", None]}}},
                {"$group": {"_id": None, **counters}},
            ]
        )
    )
    counts = rows[0] if rows else {}
    return [
        {
            "value": option["value"],
            "label": option["label"],
            "count": int(counts.get(option["value"], 0)),
        }
        for option in CATALOG_FRESHNESS_OPTIONS
    ]

def _catalog_facets(
    db: Any,
    *,
    q: str | None,
    freshness: str | None,
    work_modes: list[str] | tuple[str, ...] | None,
    employment_types: list[str] | tuple[str, ...] | None,
    locations: list[str] | tuple[str, ...] | None,
    companies: list[str] | tuple[str, ...] | None,
    skills: list[str] | tuple[str, ...] | None,
) -> dict[str, list[dict[str, Any]]]:
    common = {
        "q": q,
        "freshness": freshness,
        "work_modes": work_modes,
        "employment_types": employment_types,
        "locations": locations,
        "companies": companies,
        "skills": skills,
    }
    return {
        "freshness": _freshness_facets(db, _build_catalog_query(**common, include_freshness=False)),
        "work_modes": _facet_counts_for_work_modes(db, _build_catalog_query(**common, include_work_modes=False)),
        "employment_types": _facet_counts_for_field(db, _build_catalog_query(**common, include_employment_types=False), "employment_type"),
        "locations": _facet_counts_for_field(db, _build_catalog_query(**common, include_locations=False), "location_text"),
        "companies": _facet_counts_for_field(db, _build_catalog_query(**common, include_companies=False), "company"),
        "skills": _facet_counts_for_skills(db, _build_catalog_query(**common, include_skills=False)),
    }


def _catalog_sort_pipeline(sort: str) -> dict[str, int]:
    selected = _normalise_sort(sort)
    if selected == "oldest":
        return {"_catalog_freshness_at": 1, "title": 1, "company": 1, "job_id": 1}
    if selected == "title_asc":
        return {"title": 1, "company": 1, "job_id": 1}
    if selected == "title_desc":
        return {"title": -1, "company": 1, "job_id": 1}
    return {"_catalog_freshness_at": -1, "title": 1, "company": 1, "job_id": 1}


def list_all_jobs_catalog(
    *,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    freshness: str | None = None,
    work_modes: list[str] | tuple[str, ...] | None = None,
    employment_types: list[str] | tuple[str, ...] | None = None,
    locations: list[str] | tuple[str, ...] | None = None,
    companies: list[str] | tuple[str, ...] | None = None,
    skills: list[str] | tuple[str, ...] | None = None,
    sort: str | None = "newest",
) -> dict[str, Any]:
    """Return the paginated candidate-facing job catalog and dynamic filter facets.

    Freshness uses a source-provided posting date when extraction captured one.
    Older warehouse rows that do not have that value fall back to ``first_seen_at``
    so timeline filtering remains immediately useful without fabricating a date.
    """
    db = get_mongo_database()
    safe_limit = min(max(int(limit or 50), 1), 100)
    safe_offset = max(int(offset or 0), 0)
    selected_freshness = _normalise_freshness(freshness)
    selected_work_modes = _normalise_work_modes(work_modes)
    selected_employment_types = _normalise_filter_values(employment_types)
    selected_locations = _normalise_filter_values(locations)
    selected_companies = _normalise_filter_values(companies)
    selected_skills = _normalise_filter_values(skills)
    selected_sort = _normalise_sort(sort)
    selected_query = str(q or "").strip()

    query = _build_catalog_query(
        q=selected_query,
        freshness=selected_freshness,
        work_modes=selected_work_modes,
        employment_types=selected_employment_types,
        locations=selected_locations,
        companies=selected_companies,
        skills=selected_skills,
    )
    total = int(db[MongoCollections.JOBS_CURRENT].count_documents(query))
    rows = list(
        db[MongoCollections.JOBS_CURRENT].aggregate(
            [
                {"$match": query},
                {"$addFields": {"_catalog_freshness_at": _catalog_timestamp_expression()}},
                {"$sort": _catalog_sort_pipeline(selected_sort)},
                {"$skip": safe_offset},
                {"$limit": safe_limit},
                {"$project": {**_job_catalog_projection(), "_catalog_freshness_at": 1}},
            ]
        )
    )

    jobs: list[dict[str, Any]] = []
    for row in rows:
        raw_job = _clean_doc(row) or {}
        job = _job_snapshot(raw_job)
        if not _is_candidate_visible_job({**raw_job, **job}):
            continue
        job["source_collection"] = MongoCollections.JOBS_CURRENT
        jobs.append(job)

    filter_args = {
        "q": selected_query,
        "freshness": selected_freshness,
        "work_modes": selected_work_modes,
        "employment_types": selected_employment_types,
        "locations": selected_locations,
        "companies": selected_companies,
        "skills": selected_skills,
    }
    next_offset = safe_offset + safe_limit if safe_offset + safe_limit < total else None
    previous_offset = max(safe_offset - safe_limit, 0) if safe_offset > 0 else None

    return {
        "jobs": jobs,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "next_offset": next_offset,
        "previous_offset": previous_offset,
        "q": selected_query,
        "sort": selected_sort,
        "filters": {key: value for key, value in filter_args.items() if key != "q"},
        "facets": _catalog_facets(db, **filter_args),
        "freshness_note": "Freshness uses the source posting date when available; otherwise it uses the date Job Miner first discovered the job.",
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
