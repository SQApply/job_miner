from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from ..common.constants import MongoCollections
from ..infrastructure.mongo import get_mongo_database
from ..observability.correlation import new_uuid
from ..tasks.recommendation_tasks import generate_recommendations_after_resume_upload_task
from ..tasks.tracking import create_task_tracking_row, mark_task_failed

PROFILE_EDIT_EVENTS_COLLECTION = "candidate_profile_edit_events"

MATCHING_IMPACT_FIELDS = {
    "location",
    "total_experience_years",
    "skills",
    "domains",
    "current_title",
    "current_company",
    "target_roles",
    "preferred_locations",
    "remote_preference",
    "employment_type",
    "seniority_level",
    "summary",
}

EDITABLE_FIELDS = MATCHING_IMPACT_FIELDS | {
    "phone",
    "linkedin_url",
    "github_url",
    "portfolio_url",
    "notice_period",
    "expected_compensation",
    "summary",
}

LOCKED_FIELDS = {"full_name", "name", "email", "candidate_id", "resume_id", "sha256", "source_file_name"}

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,79}$")
_PHONE_RE = re.compile(r"^[+0-9][0-9\s().-]{6,24}$")
_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_hash(value: Any) -> str:
    normalized = json.dumps(value or {}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _clean_text(value: Any, *, max_len: int = 300) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return re.sub(r"\s+", " ", text)[:max_len]


def _normalize_list(values: Any, *, max_items: int = 80, max_item_len: int = 80) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = re.split(r"[,\n;]+", values)
    elif isinstance(values, list):
        raw_items = values
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="List fields must be arrays or comma-separated strings.")

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = _clean_text(item, max_len=max_item_len)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def _normalize_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Experience must be a valid number of years.")
    if parsed < 0 or parsed > 60:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Experience must be between 0 and 60 years.")
    return round(parsed, 1)


def _validate_url(value: Any, field_name: str) -> str | None:
    text = _clean_text(value, max_len=500)
    if not text:
        return None
    if not _URL_RE.match(text):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field_name} must start with http:// or https://.")
    return text


def _validate_name_email_lock(payload: dict[str, Any], tower: dict[str, Any]) -> None:
    supplied_name = _clean_text(payload.get("full_name") or payload.get("name"))
    supplied_email = _clean_text(payload.get("email"))
    current_name = _clean_text(tower.get("full_name"))
    current_email = _clean_text(tower.get("email"))

    if supplied_name and current_name and supplied_name.casefold() != current_name.casefold():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name cannot be changed from the candidate profile editor. Contact support to update it.")
    if supplied_email and current_email and supplied_email.casefold() != current_email.casefold():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email cannot be changed from the candidate profile editor. Use account settings or contact support.")


def normalize_profile_patch(payload: dict[str, Any], tower: dict[str, Any]) -> dict[str, Any]:
    _validate_name_email_lock(payload, tower)

    disallowed = sorted(set(payload) & LOCKED_FIELDS)
    # Allow unchanged name/email values from read-only form posts, but reject other locked identifiers.
    disallowed = [field for field in disallowed if field not in {"full_name", "name", "email"}]
    if disallowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"These fields cannot be edited: {', '.join(disallowed)}")

    updates: dict[str, Any] = {}
    if "phone" in payload:
        phone = _clean_text(payload.get("phone"), max_len=40)
        if phone and not _PHONE_RE.match(phone):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Phone number format is invalid.")
        updates["phone"] = phone
    if "location" in payload:
        updates["location"] = _clean_text(payload.get("location"), max_len=160)
    if "current_title" in payload:
        updates["current_title"] = _clean_text(payload.get("current_title"), max_len=160)
    if "current_company" in payload:
        updates["current_company"] = _clean_text(payload.get("current_company"), max_len=160)
    if "total_experience_years" in payload:
        updates["total_experience_years"] = _normalize_float(payload.get("total_experience_years"))
    if "skills" in payload:
        updates["skills"] = _normalize_list(payload.get("skills"), max_items=120)
    if "domains" in payload:
        updates["domains"] = _normalize_list(payload.get("domains"), max_items=40)
    if "target_roles" in payload:
        updates["target_roles"] = _normalize_list(payload.get("target_roles"), max_items=30)
    if "preferred_locations" in payload:
        updates["preferred_locations"] = _normalize_list(payload.get("preferred_locations"), max_items=40)
    if "remote_preference" in payload:
        value = _clean_text(payload.get("remote_preference"), max_len=40)
        allowed = {None, "remote", "hybrid", "onsite", "no_preference"}
        if value and value not in allowed:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="remote_preference must be remote, hybrid, onsite, or no_preference.")
        updates["remote_preference"] = value
    if "employment_type" in payload:
        updates["employment_type"] = _clean_text(payload.get("employment_type"), max_len=80)
    if "seniority_level" in payload:
        updates["seniority_level"] = _clean_text(payload.get("seniority_level"), max_len=80)
    if "linkedin_url" in payload:
        updates["linkedin_url"] = _validate_url(payload.get("linkedin_url"), "LinkedIn URL")
    if "github_url" in payload:
        updates["github_url"] = _validate_url(payload.get("github_url"), "GitHub URL")
    if "portfolio_url" in payload:
        updates["portfolio_url"] = _validate_url(payload.get("portfolio_url"), "Portfolio URL")
    if "notice_period" in payload:
        updates["notice_period"] = _clean_text(payload.get("notice_period"), max_len=80)
    if "expected_compensation" in payload:
        updates["expected_compensation"] = _clean_text(payload.get("expected_compensation"), max_len=120)
    if "summary" in payload:
        updates["summary"] = _clean_text(payload.get("summary"), max_len=2000)

    unknown = sorted(set(payload) - EDITABLE_FIELDS - LOCKED_FIELDS - {"name"})
    if unknown:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported profile fields: {', '.join(unknown)}")

    return updates


def effective_profile_from_tower(tower: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    overrides = overrides or {}
    fields = [
        "candidate_id",
        "resume_id",
        "full_name",
        "email",
        "phone",
        "location",
        "current_title",
        "current_company",
        "total_experience_years",
        "skills",
        "domains",
        "target_roles",
        "preferred_locations",
        "remote_preference",
        "employment_type",
        "seniority_level",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        "notice_period",
        "expected_compensation",
        "summary",
    ]
    effective: dict[str, Any] = {}
    for field in fields:
        if field in {"candidate_id", "resume_id", "full_name", "email"}:
            effective[field] = tower.get(field)
        elif field in overrides:
            effective[field] = overrides.get(field)
        else:
            effective[field] = tower.get(field)
    effective["skills"] = _normalize_list(effective.get("skills"), max_items=120)
    effective["domains"] = _normalize_list(effective.get("domains"), max_items=40)
    effective["target_roles"] = _normalize_list(effective.get("target_roles"), max_items=30)
    effective["preferred_locations"] = _normalize_list(effective.get("preferred_locations"), max_items=40)
    return effective


def matching_input(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "location": profile.get("location"),
        "total_experience_years": profile.get("total_experience_years"),
        "skills": profile.get("skills") or [],
        "domains": profile.get("domains") or [],
        "current_title": profile.get("current_title"),
        "current_company": profile.get("current_company"),
        "target_roles": profile.get("target_roles") or [],
        "preferred_locations": profile.get("preferred_locations") or [],
        "remote_preference": profile.get("remote_preference"),
        "employment_type": profile.get("employment_type"),
        "seniority_level": profile.get("seniority_level"),
    }


def matching_hash(profile: dict[str, Any]) -> str:
    return _json_hash(matching_input(profile))


def _join(values: list[str]) -> str:
    return ", ".join([str(value) for value in values if value])


def build_effective_text_fields(tower: dict[str, Any], effective: dict[str, Any]) -> dict[str, str]:
    identity_lines = [
        str(effective.get("full_name") or ""),
        str(effective.get("current_title") or ""),
        str(effective.get("current_company") or ""),
        str(effective.get("location") or ""),
        f"Total experience: {effective.get('total_experience_years')} years" if effective.get("total_experience_years") is not None else "",
        f"Remote preference: {effective.get('remote_preference')}" if effective.get("remote_preference") else "",
        f"Preferred locations: {_join(effective.get('preferred_locations') or [])}" if effective.get("preferred_locations") else "",
        f"Target roles: {_join(effective.get('target_roles') or [])}" if effective.get("target_roles") else "",
    ]
    identity_text = "\n".join([line for line in identity_lines if line]).strip()

    skills_text = "\n".join([
        "Skills: " + _join(effective.get("skills") or []),
        "Domains: " + _join(effective.get("domains") or []),
    ]).strip()

    candidate_embedding_text = "\n\n".join([
        text for text in [
            identity_text,
            str(effective.get("summary") or tower.get("summary") or ""),
            skills_text,
            str(tower.get("experience_text") or ""),
            str(tower.get("education_text") or ""),
        ] if text
    ]).strip()

    return {
        "identity_text": identity_text,
        "skills_text": skills_text,
        "candidate_embedding_text": candidate_embedding_text,
    }


def diff_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = sorted(set(before) | set(after))
    changed: list[str] = []
    for key in keys:
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def _queue_profile_edit_recommendations(
    *,
    db: Any,
    candidate_id: str,
    resume_id: str | None,
    email: str | None,
    matching_input_hash: str,
    request_id: str | None,
    app_user_id: str | None,
) -> dict[str, Any]:
    if os.getenv("JOB_MINER_ENABLE_AUTO_RECOMMENDATION_QUEUE", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"status": "disabled", "message": "Automatic recommendation generation is disabled."}

    queue_name = os.getenv("JOB_MINER_RECOMMENDATION_QUEUE", "recommendation_queue")
    run_session_id = f"candidate_profile_edit_{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    now = utc_now()

    skip_duplicate = os.getenv("JOB_MINER_PROFILE_EDIT_SKIP_DUPLICATE_RECOMMENDATIONS", "true").strip().lower() in {"1", "true", "yes", "on"}
    if skip_duplicate:
        current = db[MongoCollections.CANDIDATE_TOWER_RECORDS].find_one(
            {"candidate_id": candidate_id},
            {"recommendation_status": 1, "recommendation_task_id": 1, "recommendation_queue": 1, "recommendation_requested_for_hash": 1},
        ) or {}
        if (
            current.get("recommendation_status") in {"queued", "running"}
            and current.get("recommendation_requested_for_hash") == matching_input_hash
        ):
            return {
                "status": "already_queued",
                "task_id": current.get("recommendation_task_id"),
                "queue": current.get("recommendation_queue") or queue_name,
                "message": "Recommendation refresh is already queued or running for this profile version.",
            }

    task_id = new_uuid()
    task_payload = {
        "request_id": request_id,
        "candidate_id": candidate_id,
        "resume_id": resume_id,
        "email": email,
        "run_session_id": run_session_id,
        "source": "candidate_profile_edit",
        "matching_input_hash": matching_input_hash,
        "app_user_id": app_user_id,
    }

    create_task_tracking_row(
        task_uuid=task_id,
        task_name="src.tasks.recommendation_tasks.generate_recommendations_after_resume_upload_task",
        queue_name=queue_name,
        user={"id": app_user_id} if app_user_id else None,
        payload=task_payload,
    )

    try:
        async_result = generate_recommendations_after_resume_upload_task.apply_async(
            kwargs={
                "candidate_id": candidate_id,
                "resume_id": resume_id,
                "email": email,
                "run_session_id": run_session_id,
                "source": "candidate_profile_edit",
                "request_id": request_id,
            },
            queue=queue_name,
            task_id=task_id,
        )
    except Exception as exc:
        mark_task_failed(
            task_uuid=task_id,
            error=exc,
            entity_type="recommendation_generation",
            entity_id=candidate_id,
            failed_payload=task_payload,
            message="Recommendation refresh could not be queued after profile edit.",
        )
        db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
            {"candidate_id": candidate_id},
            {"$set": {
                "recommendation_status": "queue_failed",
                "recommendation_status_message": "Profile was updated, but recommendation refresh could not be queued.",
                "recommendation_error": repr(exc),
                "recommendation_task_id": task_id,
                "recommendation_queue": queue_name,
                "recommendation_request_id": request_id,
                "recommendation_updated_at": now,
            }},
        )
        return {"status": "queue_failed", "message": "Recommendation refresh could not be queued.", "error": repr(exc), "task_id": task_id}

    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
        {"candidate_id": candidate_id},
        {"$set": {
            "recommendation_status": "queued",
            "recommendation_status_message": "Profile updated. Recommendations are refreshing in the background.",
            "recommendation_task_id": async_result.id,
            "recommendation_queue": queue_name,
            "recommendation_request_id": request_id,
            "recommendation_queued_at": now,
            "recommendation_updated_at": now,
            "recommendation_error": None,
            "recommendation_requested_for_hash": matching_input_hash,
        }},
    )
    return {"status": "queued", "task_id": async_result.id, "queue": queue_name, "run_session_id": run_session_id, "request_id": request_id}


def update_candidate_profile(*, candidate_id: str, app_user_id: str, user_email: str | None, payload: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
    db = get_mongo_database()
    tower = db[MongoCollections.CANDIDATE_TOWER_RECORDS].find_one({"candidate_id": candidate_id})
    if not tower:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate profile not found.")

    current_overrides = dict(tower.get("user_overrides") or {})
    normalized_updates = normalize_profile_patch(payload, tower)
    if not normalized_updates:
        effective = effective_profile_from_tower(tower, current_overrides)
        return {
            "saved": False,
            "candidate_id": candidate_id,
            "changed_fields": [],
            "matching_impacting_fields_changed": [],
            "recommendations_refresh_required": False,
            "profile": effective,
        }

    before_effective = effective_profile_from_tower(tower, current_overrides)
    before_hash = str(tower.get("candidate_matching_input_hash") or matching_hash(before_effective))

    new_overrides = dict(current_overrides)
    new_overrides.update(normalized_updates)
    after_effective = effective_profile_from_tower(tower, new_overrides)
    after_hash = matching_hash(after_effective)
    changed = diff_fields(before_effective, after_effective)
    matching_changed_fields = [field for field in changed if field in MATCHING_IMPACT_FIELDS]
    matching_changed = before_hash != after_hash and bool(matching_changed_fields)

    text_fields = build_effective_text_fields(tower, after_effective)
    now = utc_now()
    version = int(tower.get("profile_version") or 0) + 1
    edit_id = str(uuid.uuid4())

    update_doc: dict[str, Any] = {
        **{field: after_effective.get(field) for field in [
            "phone",
            "location",
            "current_title",
            "current_company",
            "total_experience_years",
            "skills",
            "domains",
            "target_roles",
            "preferred_locations",
            "remote_preference",
            "employment_type",
            "seniority_level",
            "linkedin_url",
            "github_url",
            "portfolio_url",
            "notice_period",
            "expected_compensation",
            "summary",
        ]},
        **text_fields,
        "effective_profile": after_effective,
        "user_overrides": new_overrides,
        "profile_version": version,
        "profile_last_edited_at": now,
        "profile_last_edited_by_app_user_id": app_user_id,
        "candidate_matching_input": matching_input(after_effective),
        "candidate_matching_input_hash": after_hash,
        "updated_at": now,
    }
    if matching_changed:
        update_doc.update({
            "embedding_status": "pending",
            "embedding_model": None,
            "last_indexed_at": None,
            "source_content_hash": after_hash,
            "last_recommendation_input_hash": after_hash,
        })

    parsed_snapshot = {key: value for key, value in tower.items() if key != "_id"}

    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
        {"candidate_id": candidate_id},
        {"$set": update_doc},
    )
    # Preserve the first parsed snapshot separately. This update is separate to avoid overwriting it on later edits.
    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
        {"candidate_id": candidate_id, "parsed_candidate_tower_snapshot": {"$exists": False}},
        {"$set": {"parsed_candidate_tower_snapshot": parsed_snapshot}},
    )

    event_doc = {
        "edit_id": edit_id,
        "candidate_id": candidate_id,
        "resume_id": tower.get("resume_id"),
        "app_user_id": app_user_id,
        "user_email": user_email,
        "changed_fields": changed,
        "matching_impacting_fields_changed": matching_changed_fields,
        "before_effective_profile": before_effective,
        "after_effective_profile": after_effective,
        "updates": normalized_updates,
        "matching_hash_before": before_hash,
        "matching_hash_after": after_hash,
        "recommendations_refresh_required": matching_changed,
        "created_at": now,
    }
    db[PROFILE_EDIT_EVENTS_COLLECTION].insert_one(event_doc)

    recommendation_generation = None
    if matching_changed:
        recommendation_generation = _queue_profile_edit_recommendations(
            db=db,
            candidate_id=candidate_id,
            resume_id=tower.get("resume_id"),
            email=tower.get("email"),
            matching_input_hash=after_hash,
            request_id=request_id,
            app_user_id=app_user_id,
        )

    return {
        "saved": True,
        "candidate_id": candidate_id,
        "resume_id": tower.get("resume_id"),
        "profile_version": version,
        "changed_fields": changed,
        "matching_impacting_fields_changed": matching_changed_fields,
        "recommendations_refresh_required": matching_changed,
        "recommendation_generation": recommendation_generation,
        "profile": after_effective,
    }
