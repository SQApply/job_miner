from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, IndexModel
from pymongo.database import Database


FEEDBACK_COLLECTION = "candidate_job_feedback"
TRAINING_PAIR_COLLECTION = "candidate_job_training_pairs"


VALID_FEEDBACK_LABELS = {
    "accepted",
    "shortlisted",
    "applied",
    "good_match",
    "rejected",
    "irrelevant",
    "bad_match",
    "neutral",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def init_optimization_indexes(db: Database) -> dict[str, int]:
    indexes: dict[str, list[IndexModel]] = {
        "candidate_job_matches_llm_reranked": [
            IndexModel([("match_run_id", ASCENDING)]),
            IndexModel([("candidate_id", ASCENDING), ("final_rank", ASCENDING)]),
            IndexModel(
                [("candidate_id", ASCENDING), ("job_id", ASCENDING), ("match_run_id", ASCENDING)],
                unique=True,
            ),
            IndexModel([("final_score_0_100", ASCENDING)]),
        ],
        FEEDBACK_COLLECTION: [
            IndexModel([("feedback_id", ASCENDING)], unique=True),
            IndexModel([("candidate_id", ASCENDING), ("job_id", ASCENDING)]),
            IndexModel([("label", ASCENDING)]),
            IndexModel([("source", ASCENDING)]),
        ],
        TRAINING_PAIR_COLLECTION: [
            IndexModel([("training_pair_id", ASCENDING)], unique=True),
            IndexModel([("candidate_id", ASCENDING), ("job_id", ASCENDING)]),
            IndexModel([("label", ASCENDING)]),
        ],
    }

    out: dict[str, int] = {}

    for collection_name, collection_indexes in indexes.items():
        db[collection_name].create_indexes(collection_indexes)
        out[collection_name] = len(collection_indexes)

    return out


def add_feedback(
    *,
    db: Database,
    candidate_id: str,
    job_id: str,
    label: str,
    match_run_id: str | None = None,
    reason: str | None = None,
    source: str = "manual",
    created_by: str | None = None,
) -> dict[str, Any]:
    label = label.strip().lower()

    if label not in VALID_FEEDBACK_LABELS:
        raise ValueError(
            f"Invalid label={label}. Valid labels are: {sorted(VALID_FEEDBACK_LABELS)}"
        )

    feedback_id = "fb_" + _stable_hash(
        {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "match_run_id": match_run_id,
            "source": source,
        }
    )[:24]

    now = _utc_now()

    record = {
        "feedback_id": feedback_id,
        "candidate_id": candidate_id,
        "job_id": job_id,
        "match_run_id": match_run_id,
        "label": label,
        "reason": reason,
        "source": source,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
    }

    db[FEEDBACK_COLLECTION].update_one(
        {"feedback_id": feedback_id},
        {
            "$set": record,
            "$setOnInsert": {
                "_id": feedback_id,
            },
        },
        upsert=True,
    )

    return record


def _label_to_binary(label: str) -> int | None:
    positive = {"accepted", "shortlisted", "applied", "good_match"}
    negative = {"rejected", "irrelevant", "bad_match"}

    if label in positive:
        return 1

    if label in negative:
        return 0

    return None


def export_training_pairs(
    *,
    db: Database,
    output_path: Path = Path("data/processed/training/candidate_job_training_pairs_latest.jsonl"),
) -> dict[str, Any]:
    feedback_rows = list(db[FEEDBACK_COLLECTION].find({}))

    records: list[dict[str, Any]] = []

    for row in feedback_rows:
        label = str(row.get("label") or "").lower()
        binary_label = _label_to_binary(label)

        if binary_label is None:
            continue

        candidate_id = row.get("candidate_id")
        job_id = row.get("job_id")

        candidate = db["candidate_tower_records"].find_one(
            {"candidate_id": candidate_id}
        )

        job = db["jobs_current"].find_one(
            {"job_id": job_id}
        )

        if not candidate or not job:
            continue

        training_pair_id = "tp_" + _stable_hash(
            {
                "candidate_id": candidate_id,
                "job_id": job_id,
                "label": binary_label,
                "feedback_id": row.get("feedback_id"),
            }
        )[:24]

        record = {
            "training_pair_id": training_pair_id,
            "candidate_id": candidate_id,
            "resume_id": candidate.get("resume_id"),
            "job_id": job_id,
            "label": binary_label,
            "label_name": label,
            "feedback_id": row.get("feedback_id"),
            "feedback_source": row.get("source"),
            "feedback_reason": row.get("reason"),
            "candidate_text": candidate.get("candidate_embedding_text"),
            "job_text": job.get("summary") or job.get("raw_payload") or job.get("title"),
            "candidate_features": {
                "full_name": candidate.get("full_name"),
                "current_title": candidate.get("current_title"),
                "skills": candidate.get("skills") or [],
                "domains": candidate.get("domains") or [],
            },
            "job_features": {
                "title": job.get("title"),
                "company": job.get("company"),
                "location_text": job.get("location_text"),
                "required_skills": job.get("required_skills") or [],
                "preferred_skills": job.get("preferred_skills") or [],
                "employment_type": job.get("employment_type"),
            },
            "created_at": _utc_now(),
        }

        db[TRAINING_PAIR_COLLECTION].update_one(
            {"training_pair_id": training_pair_id},
            {
                "$set": record,
                "$setOnInsert": {
                    "_id": training_pair_id,
                },
            },
            upsert=True,
        )

        records.append(record)

    _write_jsonl(output_path, records)

    return {
        "feedback_rows": len(feedback_rows),
        "training_pairs_exported": len(records),
        "output_path": str(output_path),
        "collection": TRAINING_PAIR_COLLECTION,
    }