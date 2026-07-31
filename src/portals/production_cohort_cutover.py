from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


CUTOVER_CONTRACT_VERSION = "1.0"
CUTOVER_CONFIRMATION = "DEACTIVATE_OUTSIDE_PHASE7D4C_22_COHORT"
ACTIVE_REFRESH_STATUSES = ("pending", "queued", "running")

_JOB_PROJECTION = {
    "_id": 1,
    "job_id": 1,
    "target_id": 1,
    "source_id": 1,
    "is_active": 1,
    "catalog_visible": 1,
    "freshness_status": 1,
    "inactive_reason": 1,
    "deactivated_at": 1,
    "updated_at": 1,
}


class CohortCutoverError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_backup_archive(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise CohortCutoverError(
            f"Mongo backup archive is missing or empty: {path}"
        )
    try:
        with gzip.open(path, "rb") as handle:
            if not handle.read(1):
                raise CohortCutoverError(
                    f"Mongo backup archive has no payload: {path}"
                )
    except (OSError, EOFError) as exc:
        raise CohortCutoverError(
            f"Mongo backup archive is not a readable gzip archive: {path}"
        ) from exc


def portal_identity(document: dict[str, Any]) -> str:
    target_id = str(document.get("target_id") or "").strip()
    if target_id:
        return target_id
    source_id = str(document.get("source_id") or "").strip()
    return source_id or "unknown"


def load_cutover_policy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    source_ids = [
        str(value or "").strip()
        for value in payload.get("source_ids") or []
    ]
    expected = int(payload.get("expected_source_count") or 0)
    blockers: list[str] = []
    if payload.get("contract_version") != CUTOVER_CONTRACT_VERSION:
        blockers.append("unsupported_contract_version")
    if payload.get("phase") != "7D4C":
        blockers.append("unexpected_phase")
    if not source_ids:
        blockers.append("empty_source_ids")
    if any(not value for value in source_ids):
        blockers.append("blank_source_id")
    if len(source_ids) != len(set(source_ids)):
        blockers.append("duplicate_source_ids")
    if expected != len(source_ids):
        blockers.append("expected_source_count_mismatch")
    if expected != 22:
        blockers.append("cutover_must_contain_exactly_22_sources")
    controls = payload.get("controls") or {}
    if controls.get("physical_job_deletion_enabled") is not False:
        blockers.append("physical_job_deletion_must_be_disabled")
    if controls.get("automatic_scheduler_enabled") is not False:
        blockers.append("automatic_scheduler_must_be_disabled")
    if blockers:
        raise CohortCutoverError(
            "Invalid Phase 7D4C cutover policy: " + ",".join(blockers)
        )
    payload["source_ids"] = source_ids
    payload["policy_sha256"] = _sha256(
        {key: value for key, value in payload.items() if key != "policy_sha256"}
    )
    return payload


def classify_job_documents(
    documents: Iterable[dict[str, Any]],
    *,
    cohort_source_ids: Sequence[str],
) -> dict[str, Any]:
    cohort = set(cohort_source_ids)
    rows = list(documents)
    identity_totals: Counter[str] = Counter()
    identity_active: Counter[str] = Counter()
    cohort_total = 0
    cohort_active = 0
    outside_total = 0
    outside_active = 0
    outside_inactive = 0
    outside_rows: list[dict[str, Any]] = []
    outside_active_rows: list[dict[str, Any]] = []

    fingerprint_rows: list[dict[str, Any]] = []
    for document in rows:
        identity = portal_identity(document)
        active = document.get("is_active") is not False
        identity_totals[identity] += 1
        if active:
            identity_active[identity] += 1
        if identity in cohort:
            cohort_total += 1
            if active:
                cohort_active += 1
        else:
            outside_total += 1
            outside_rows.append(document)
            if active:
                outside_active += 1
                outside_active_rows.append(document)
            else:
                outside_inactive += 1
        fingerprint_rows.append(
            {
                "mongo_id": str(document.get("_id") or ""),
                "job_id": str(document.get("job_id") or ""),
                "identity": identity,
                "active": active,
            }
        )

    populated = sorted(
        source_id for source_id in cohort if identity_totals[source_id] > 0
    )
    active_sources = sorted(
        source_id for source_id in cohort if identity_active[source_id] > 0
    )
    missing = sorted(cohort.difference(populated))
    zero_active = sorted(cohort.difference(active_sources))
    outside_active_job_ids = sorted(
        {
            str(row.get("job_id") or "").strip()
            for row in outside_active_rows
            if str(row.get("job_id") or "").strip()
        }
    )
    cohort_active_job_ids = sorted(
        {
            str(row.get("job_id") or "").strip()
            for row in rows
            if portal_identity(row) in cohort
            and row.get("is_active") is not False
            and str(row.get("job_id") or "").strip()
        }
    )
    cohort_active_missing_job_id = sum(
        1
        for row in rows
        if portal_identity(row) in cohort
        and row.get("is_active") is not False
        and not str(row.get("job_id") or "").strip()
    )
    outside_job_ids = sorted(
        {
            str(row.get("job_id") or "").strip()
            for row in outside_rows
            if str(row.get("job_id") or "").strip()
        }
    )
    outside_active_missing_job_id = sum(
        1
        for row in outside_active_rows
        if not str(row.get("job_id") or "").strip()
    )
    outside_missing_job_id = sum(
        1
        for row in outside_rows
        if not str(row.get("job_id") or "").strip()
    )
    return {
        "jobs_current_total": len(rows),
        "jobs_current_active": sum(
            1 for row in rows if row.get("is_active") is not False
        ),
        "jobs_current_inactive": sum(
            1 for row in rows if row.get("is_active") is False
        ),
        "cohort_total": cohort_total,
        "cohort_active": cohort_active,
        "cohort_inactive": cohort_total - cohort_active,
        "outside_total": outside_total,
        "outside_active": outside_active,
        "outside_inactive": outside_inactive,
        "expected_active_after_cutover": cohort_active,
        "expected_inactive_after_cutover": len(rows) - cohort_active,
        "populated_source_ids": populated,
        "active_source_ids": active_sources,
        "missing_source_ids": missing,
        "zero_active_source_ids": zero_active,
        "outside_active_job_ids": outside_active_job_ids,
        "outside_job_ids": outside_job_ids,
        "cohort_active_job_ids": cohort_active_job_ids,
        "cohort_active_missing_job_id": cohort_active_missing_job_id,
        "outside_active_missing_job_id": outside_active_missing_job_id,
        "outside_missing_job_id": outside_missing_job_id,
        "outside_active_by_identity": dict(
            sorted(
                (
                    (identity, count)
                    for identity, count in identity_active.items()
                    if identity not in cohort
                ),
                key=lambda item: (-item[1], item[0]),
            )
        ),
        "state_sha256": _sha256(
            sorted(
                fingerprint_rows,
                key=lambda row: (
                    row["identity"],
                    row["job_id"],
                    row["mongo_id"],
                ),
            )
        ),
        "_outside_active_rows": outside_active_rows,
    }


def build_cutover_plan(
    database: Any,
    *,
    policy: dict[str, Any],
) -> dict[str, Any]:
    jobs = list(database["jobs_current"].find({}, _JOB_PROJECTION))
    classified = classify_job_documents(
        jobs,
        cohort_source_ids=policy["source_ids"],
    )
    retained_ids = classified["cohort_active_job_ids"]
    derived = {
        "job_tower_records_to_delete": int(
            database["job_tower_records"].count_documents(
                {"job_id": {"$nin": retained_ids}}
            )
        ),
        "job_qdrant_index_state_to_reset": int(
            database["qdrant_index_state"].count_documents(
                {"record_type": "job"}
            )
        ),
        "candidate_job_matches_to_delete": int(
            database["candidate_job_matches"].count_documents(
                {"job_id": {"$nin": retained_ids}}
            )
        ),
        "candidate_job_matches_llm_reranked_to_delete": int(
            database["candidate_job_matches_llm_reranked"].count_documents(
                {"job_id": {"$nin": retained_ids}}
            )
        ),
        "pending_refresh_requests_to_cancel": int(
            database["recommendation_refresh_requests"].count_documents(
                {
                    "target_id": {"$nin": policy["source_ids"]},
                    "status": {"$in": list(ACTIVE_REFRESH_STATUSES)},
                }
            )
        ),
    }
    blockers: list[str] = []
    if classified["missing_source_ids"]:
        blockers.append("cohort_sources_missing_from_jobs_current")
    if classified["zero_active_source_ids"]:
        blockers.append("cohort_sources_without_active_jobs")
    if classified["cohort_active_missing_job_id"]:
        blockers.append("active_cohort_jobs_missing_job_id")
    if classified["outside_active"] <= 0:
        blockers.append("no_active_non_cohort_jobs")

    public_counts = {
        key: value
        for key, value in classified.items()
        if not key.startswith("_")
        and key
        not in {
            "outside_active_job_ids",
            "outside_job_ids",
            "cohort_active_job_ids",
        }
    }
    return {
        "contract_version": CUTOVER_CONTRACT_VERSION,
        "phase": "7D4C",
        "mode": "plan",
        "generated_at": _utc_now().isoformat(),
        "database": database.name,
        "cohort_name": policy["cohort_name"],
        "policy_sha256": policy["policy_sha256"],
        "cohort_source_ids": list(policy["source_ids"]),
        "counts": public_counts,
        "derived_cleanup": derived,
        "state_sha256": classified["state_sha256"],
        "ready_to_apply": not blockers,
        "blockers": blockers,
        "controls": {
            "mongo_reads_performed": True,
            "mongo_writes_performed": False,
            "normalized_jobs_deleted": False,
            "job_history_deleted": False,
            "raw_evidence_deleted": False,
            "automatic_scheduler_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "backup_required_before_apply": True,
            "explicit_confirmation_required": True,
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            default=_json_default,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def read_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("contract_version") != CUTOVER_CONTRACT_VERSION:
        raise CohortCutoverError("Unsupported cutover plan contract")
    if payload.get("phase") != "7D4C" or payload.get("mode") != "plan":
        raise CohortCutoverError("Not a Phase 7D4C cutover plan")
    if not payload.get("ready_to_apply"):
        raise CohortCutoverError(
            "Cutover plan has blockers: "
            + ",".join(payload.get("blockers") or [])
        )
    return payload


def _write_lifecycle_snapshot(
    path: Path,
    *,
    cutover_id: str,
    rows: Sequence[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "contract_version": CUTOVER_CONTRACT_VERSION,
        "cutover_id": cutover_id,
        "created_at": _utc_now().isoformat(),
        "record_count": len(rows),
        "records": [
            {
                "mongo_id": str(row.get("_id") or ""),
                "job_id": row.get("job_id"),
                "target_id": row.get("target_id"),
                "source_id": row.get("source_id"),
                "is_active": row.get("is_active"),
                "catalog_visible": row.get("catalog_visible"),
                "freshness_status": row.get("freshness_status"),
                "inactive_reason": row.get("inactive_reason"),
                "deactivated_at": row.get("deactivated_at"),
                "updated_at": row.get("updated_at"),
            }
            for row in rows
        ],
    }
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        json.dump(
            payload,
            handle,
            indent=2,
            ensure_ascii=False,
            default=_json_default,
        )
        handle.write("\n")


def _batches(values: Sequence[Any], size: int = 500) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def execute_cutover(
    database: Any,
    *,
    policy: dict[str, Any],
    plan: dict[str, Any],
    backup_archive: Path,
    snapshot_path: Path,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != CUTOVER_CONFIRMATION:
        raise CohortCutoverError("Exact cutover confirmation token is required")
    validate_backup_archive(backup_archive)
    if plan.get("policy_sha256") != policy["policy_sha256"]:
        raise CohortCutoverError("Policy changed after the plan was created")

    current = build_cutover_plan(database, policy=policy)
    if current["state_sha256"] != plan.get("state_sha256"):
        raise CohortCutoverError(
            "jobs_current changed after planning; create a fresh plan and backup"
        )
    if current["counts"] != plan.get("counts"):
        raise CohortCutoverError(
            "Cutover counts changed after planning; create a fresh plan"
        )
    if current["derived_cleanup"] != plan.get("derived_cleanup"):
        raise CohortCutoverError(
            "Derived collections changed after planning; create a fresh plan"
        )
    if not current["ready_to_apply"]:
        raise CohortCutoverError(
            "Live cutover preflight failed: "
            + ",".join(current.get("blockers") or [])
        )

    live_jobs = list(database["jobs_current"].find({}, _JOB_PROJECTION))
    classified = classify_job_documents(
        live_jobs,
        cohort_source_ids=policy["source_ids"],
    )
    outside_rows = classified["_outside_active_rows"]
    retained_job_ids = classified["cohort_active_job_ids"]
    outside_mongo_ids = [row["_id"] for row in outside_rows]
    cutover_id = (
        "phase7d4c_"
        + _utc_now().strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + current["state_sha256"][:10]
    )
    _write_lifecycle_snapshot(
        snapshot_path,
        cutover_id=cutover_id,
        rows=outside_rows,
    )

    now = _utc_now()
    modified = 0
    for batch in _batches(outside_mongo_ids):
        result = database["jobs_current"].update_many(
            {"_id": {"$in": batch}, "is_active": {"$ne": False}},
            {
                "$set": {
                    "is_active": False,
                    "catalog_visible": False,
                    "freshness_status": "inactive",
                    "inactive_reason": policy["deactivation"]["reason"],
                    "deactivated_at": now,
                    "updated_at": now,
                    "cohort_cutover_id": cutover_id,
                }
            },
        )
        modified += int(result.modified_count)

    cleanup = {
        "job_tower_records_deleted": int(
            database["job_tower_records"]
            .delete_many({"job_id": {"$nin": retained_job_ids}})
            .deleted_count
        ),
        "job_qdrant_index_state_reset": int(
            database["qdrant_index_state"]
            .delete_many({"record_type": "job"})
            .deleted_count
        ),
        "candidate_job_matches_deleted": int(
            database["candidate_job_matches"]
            .delete_many({"job_id": {"$nin": retained_job_ids}})
            .deleted_count
        ),
        "candidate_job_matches_llm_reranked_deleted": int(
            database["candidate_job_matches_llm_reranked"]
            .delete_many({"job_id": {"$nin": retained_job_ids}})
            .deleted_count
        ),
    }
    refresh_result = database["recommendation_refresh_requests"].update_many(
        {
            "target_id": {"$nin": policy["source_ids"]},
            "status": {"$in": list(ACTIVE_REFRESH_STATUSES)},
        },
        {
            "$set": {
                "status": "cancelled",
                "cancelled_reason": "phase7d4c_non_cohort_cutover",
                "cancelled_at": now,
                "updated_at": now,
            }
        },
    )
    cleanup["recommendation_refresh_requests_cancelled"] = int(
        refresh_result.modified_count
    )

    after_jobs = list(database["jobs_current"].find({}, _JOB_PROJECTION))
    after = classify_job_documents(
        after_jobs,
        cohort_source_ids=policy["source_ids"],
    )
    blockers: list[str] = []
    if modified != classified["outside_active"]:
        blockers.append("deactivated_count_mismatch")
    if after["outside_active"] != 0:
        blockers.append("active_non_cohort_jobs_remain")
    if after["cohort_active"] != classified["cohort_active"]:
        blockers.append("cohort_active_count_changed")
    if after["jobs_current_total"] != classified["jobs_current_total"]:
        blockers.append("normalized_job_count_changed")

    report = {
        "contract_version": CUTOVER_CONTRACT_VERSION,
        "phase": "7D4C",
        "mode": "apply",
        "status": "passed" if not blockers else "failed",
        "cutover_id": cutover_id,
        "generated_at": _utc_now().isoformat(),
        "database": database.name,
        "policy_sha256": policy["policy_sha256"],
        "plan_state_sha256": plan["state_sha256"],
        "backup": {
            "path": str(backup_archive),
            "size_bytes": backup_archive.stat().st_size,
            "sha256": file_sha256(backup_archive),
        },
        "snapshot": {
            "path": str(snapshot_path),
            "record_count": len(outside_rows),
            "sha256": file_sha256(snapshot_path),
        },
        "before": {
            key: value
            for key, value in classified.items()
            if not key.startswith("_")
            and key
            not in {
                "outside_active_job_ids",
                "outside_job_ids",
                "cohort_active_job_ids",
            }
        },
        "deactivated_jobs": modified,
        "derived_cleanup": cleanup,
        "after": {
            key: value
            for key, value in after.items()
            if not key.startswith("_")
            and key
            not in {
                "outside_active_job_ids",
                "outside_job_ids",
                "cohort_active_job_ids",
            }
        },
        "blockers": blockers,
        "ready_for_qdrant_rebuild": not blockers,
        "ready_for_candidate_pipeline": False,
        "required_next_action": (
            "Rebuild active job towers, recreate the Qdrant job collection, "
            "then run verify mode."
        ),
        "controls": {
            "normalized_jobs_deleted": False,
            "job_history_deleted": False,
            "raw_evidence_deleted": False,
            "automatic_scheduler_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "missing_count_updates_enabled": False,
            "mongo_backup_verified": True,
            "derived_records_deleted": True,
            "qdrant_collection_rebuild_required": True,
        },
    }
    if blockers:
        raise CohortCutoverError(
            "Cutover verification failed after writes: " + ",".join(blockers)
        )
    return report


def verify_cutover(
    database: Any,
    *,
    policy: dict[str, Any],
) -> dict[str, Any]:
    jobs = list(database["jobs_current"].find({}, _JOB_PROJECTION))
    classified = classify_job_documents(
        jobs,
        cohort_source_ids=policy["source_ids"],
    )
    blockers: list[str] = []
    if classified["outside_active"]:
        blockers.append("active_non_cohort_jobs_remain")
    if classified["missing_source_ids"]:
        blockers.append("cohort_sources_missing")
    if classified["zero_active_source_ids"]:
        blockers.append("cohort_sources_without_active_jobs")

    # Verify all derived records against every outside-cohort job, including
    # jobs that were already inactive before this cutover.
    retained_ids = classified["cohort_active_job_ids"]
    legacy_towers = int(
        database["job_tower_records"].count_documents(
            {"job_id": {"$nin": retained_ids}}
        )
    )
    retained_towers = int(
        database["job_tower_records"].count_documents(
            {"job_id": {"$in": retained_ids}}
        )
    )
    legacy_index_state = int(
        database["qdrant_index_state"].count_documents(
            {
                "record_type": "job",
                "record_id": {"$nin": retained_ids},
            }
        )
    )
    retained_index_state = int(
        database["qdrant_index_state"].count_documents(
            {
                "record_type": "job",
                "record_id": {"$in": retained_ids},
            }
        )
    )
    legacy_baseline_matches = int(
        database["candidate_job_matches"].count_documents(
            {"job_id": {"$nin": retained_ids}}
        )
    )
    legacy_reranked_matches = int(
        database["candidate_job_matches_llm_reranked"].count_documents(
            {"job_id": {"$nin": retained_ids}}
        )
    )
    if legacy_towers:
        blockers.append("non_cohort_job_towers_remain")
    if legacy_index_state:
        blockers.append("non_cohort_qdrant_index_state_remains")
    if legacy_baseline_matches:
        blockers.append("non_cohort_baseline_matches_remain")
    if legacy_reranked_matches:
        blockers.append("non_cohort_reranked_matches_remain")
    if retained_towers != len(retained_ids):
        blockers.append("cohort_job_tower_count_mismatch")
    if retained_index_state != retained_towers:
        blockers.append("cohort_qdrant_index_state_count_mismatch")
    return {
        "contract_version": CUTOVER_CONTRACT_VERSION,
        "phase": "7D4C",
        "mode": "verify",
        "status": "passed" if not blockers else "failed",
        "generated_at": _utc_now().isoformat(),
        "database": database.name,
        "active_jobs": classified["jobs_current_active"],
        "inactive_jobs": classified["jobs_current_inactive"],
        "cohort_active_jobs": classified["cohort_active"],
        "active_non_cohort_jobs": classified["outside_active"],
        "active_cohort_source_count": len(classified["active_source_ids"]),
        "cohort_job_ids": len(retained_ids),
        "cohort_job_towers": retained_towers,
        "cohort_qdrant_index_state": retained_index_state,
        "non_cohort_job_towers": legacy_towers,
        "non_cohort_qdrant_index_state": legacy_index_state,
        "non_cohort_baseline_matches": legacy_baseline_matches,
        "non_cohort_reranked_matches": legacy_reranked_matches,
        "blockers": blockers,
        "ready_for_candidate_pipeline": not blockers,
        "controls": {
            "mongo_reads_performed": True,
            "mongo_writes_performed": False,
            "automatic_scheduler_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
        },
    }
