from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from .certification import PortalInventoryEntry, read_portal_inventory
from .production_cohort_cutover import load_cutover_policy
from .production_ingestion import Phase6AIngestionPlan, Phase6ASource
from .production_runner import (
    CertificationPhase6ESourceExecutor,
    Phase6EProductionRunner,
    Phase6ERunnerConfig,
    Phase6ESourceResult,
)
from ..infrastructure.settings import load_app_settings
from ..matching.mongo_qdrant_sync import (
    build_embedder,
    build_vector_store,
    index_job_towers,
)
from ..warehouse.indexes import init_indexes
from ..warehouse.repositories import WarehouseRepository
from ..warehouse.tower_builders import build_job_tower_document
from ..warehouse.url_utils import canonical_job_urls


PHASE_7D4C_MANUAL_CONTRACT_VERSION = "1.0"
PHASE_7D4C_MANUAL_WRITE_CONFIRMATION = (
    "ENABLE_PHASE_7D4C_MANUAL_RECURRING_WRITE"
)
PHASE_7D4C_COHORT_NAME = "phase7d4c_active22"

_TERMINAL_CHECKPOINT_STATUSES = {
    "complete",
    "complete_guarded",
    "partial",
}


class ProductionManualRecurringError(RuntimeError):
    """Raised when a manual recurring cycle violates a production guard."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _mongo_datetime_as_utc(value: datetime | None) -> datetime | None:
    """Normalize BSON UTC datetimes returned by a non-tz-aware PyMongo client."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _source_attempt_run_id(
    *,
    cycle_id: str,
    position: int,
    source_id: str,
    attempt_number: int,
) -> str:
    """Return an immutable Phase 6E fleet id for one scrape attempt."""
    if attempt_number < 1:
        raise ProductionManualRecurringError(
            "Recurring source attempt_number must be at least 1"
        )
    return (
        f"{cycle_id}_{position:02d}_{source_id[-12:]}"
        f"_a{attempt_number:03d}"
    )


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Required {label} does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProductionManualRecurringError(
            f"{label} contains invalid JSON: {source}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductionManualRecurringError(
            f"{label} must contain a JSON object"
        )
    return payload


def _unique_source_ids(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ProductionManualRecurringError(f"{label} must be a JSON array")
    source_ids = [str(item or "").strip() for item in value]
    if any(not source_id for source_id in source_ids):
        raise ProductionManualRecurringError(f"{label} contains an empty id")
    if len(source_ids) != len(set(source_ids)):
        raise ProductionManualRecurringError(
            f"{label} contains duplicate source ids"
        )
    return source_ids


def _resolve(root: Path, value: str | Path) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def load_phase7d4c_manual_policy(
    *,
    root: Path,
    policy_path: Path,
) -> dict[str, Any]:
    """Load the exact 22-source manual policy and enforce all safety controls."""
    root = Path(root).resolve()
    payload = _read_json(policy_path, label="Phase 7D4C manual recurring policy")
    blockers: list[str] = []
    if payload.get("contract_version") != PHASE_7D4C_MANUAL_CONTRACT_VERSION:
        blockers.append("unsupported_contract_version")
    if payload.get("phase") != "7D4C":
        blockers.append("unexpected_phase")
    if payload.get("policy_status") != "manual_recurring_cycle_enabled":
        blockers.append("manual_policy_not_enabled")
    if int(payload.get("expected_source_count") or 0) != 22:
        blockers.append("expected_source_count_must_be_22")
    if int(payload.get("cadence_hours") or 0) != 72:
        blockers.append("cadence_hours_must_be_72")
    if int(payload.get("failure_retry_hours") or 0) < 1:
        blockers.append("failure_retry_hours_invalid")

    execution = payload.get("execution")
    lifecycle = payload.get("lifecycle")
    downstream = payload.get("downstream")
    if not isinstance(execution, dict):
        blockers.append("execution_controls_missing")
        execution = {}
    if not isinstance(lifecycle, dict):
        blockers.append("lifecycle_controls_missing")
        lifecycle = {}
    if not isinstance(downstream, dict):
        blockers.append("downstream_controls_missing")
        downstream = {}

    required_execution = {
        "automatic_scheduler_enabled": False,
        "manual_command_required": True,
        "catalog_mode": "complete_catalog",
        "incremental_detail_rescrape": True,
        "max_jobs_per_source": None,
        "source_concurrency": 1,
        "detail_concurrency": 1,
        "gpu_llm_concurrency": 1,
    }
    required_lifecycle = {
        "enabled": True,
        "require_successful_complete_snapshot": True,
        "failed_or_partial_runs_increment_missing_count": False,
        "deactivate_after_complete_misses": 2,
        "allow_empty_discovery": False,
        "physical_job_deletion_enabled": False,
    }
    required_downstream = {
        "changed_only": True,
        "build_job_towers": True,
        "index_qdrant": True,
        "remove_inactive_vectors": True,
        "rebuild_full_catalog": False,
    }
    for key, expected in required_execution.items():
        if execution.get(key) != expected:
            blockers.append(f"unsafe_execution_control:{key}")
    if int(execution.get("deep_refresh_days") or 0) < 1:
        blockers.append("unsafe_execution_control:deep_refresh_days")
    for key, expected in required_lifecycle.items():
        if lifecycle.get(key) != expected:
            blockers.append(f"unsafe_lifecycle_control:{key}")
    for key, expected in required_downstream.items():
        if downstream.get(key) != expected:
            blockers.append(f"unsafe_downstream_control:{key}")

    cohort_path = _resolve(
        root,
        str(payload.get("cohort_policy_path") or ""),
    )
    cohort = load_cutover_policy(cohort_path)
    source_ids = _unique_source_ids(cohort.get("source_ids"), label="cohort source ids")
    if len(source_ids) != 22:
        blockers.append("cohort_policy_must_contain_22_sources")
    if blockers:
        raise ProductionManualRecurringError(
            "Invalid Phase 7D4C manual recurring policy: " + ",".join(blockers)
        )

    normalized = dict(payload)
    normalized["source_ids"] = source_ids
    normalized["cohort_policy_sha256"] = str(cohort["policy_sha256"])
    normalized["policy_sha256"] = _canonical_sha256(payload)
    return normalized


def require_phase7d4c_manual_confirmation(value: str) -> None:
    if str(value or "") != PHASE_7D4C_MANUAL_WRITE_CONFIRMATION:
        raise ProductionManualRecurringError(
            "Manual recurring writes require --confirm-production-writes "
            + PHASE_7D4C_MANUAL_WRITE_CONFIRMATION
        )


def _collect_source_hints(
    *,
    root: Path,
    source_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Reuse known route/platform evidence without making it mandatory."""
    wanted = set(source_ids)
    hints: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            source_id = str(value.get("source_id") or "").strip()
            if source_id in wanted and value.get("listing_url"):
                current = hints.setdefault(source_id, {})
                for key in (
                    "listing_url",
                    "resolved_route_url",
                    "detected_platform",
                    "bounded_extracted_jobs",
                    "evidence_run_id",
                ):
                    candidate = value.get(key)
                    if candidate not in (None, "", []) and current.get(key) in (
                        None,
                        "",
                        [],
                    ):
                        current[key] = candidate
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    config_dir = root / "configs" / "portal_cohorts"
    if config_dir.is_dir():
        for path in sorted(config_dir.glob("*.json")):
            try:
                visit(json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, json.JSONDecodeError):
                continue
    return hints


def build_phase7d4c_manual_plan(
    *,
    root: Path,
    policy: Mapping[str, Any],
    inventory_path: Path,
    requested_source_ids: Sequence[str] | None = None,
    generated_at: datetime | None = None,
) -> Phase6AIngestionPlan:
    """Build a deterministic Phase 6E input plan from the live URL inventory."""
    source_ids = _unique_source_ids(policy.get("source_ids"), label="policy source ids")
    inventory = read_portal_inventory(Path(inventory_path))
    inventory_by_id = {entry.source_id: entry for entry in inventory}
    missing = [source_id for source_id in source_ids if source_id not in inventory_by_id]
    if missing:
        raise ProductionManualRecurringError(
            "The portal inventory is missing Phase 7D4C source ids: "
            + ",".join(missing)
        )

    requested = [str(value or "").strip() for value in requested_source_ids or []]
    if any(not value for value in requested) or len(requested) != len(set(requested)):
        raise ProductionManualRecurringError(
            "Requested source ids must be non-empty and unique"
        )
    rejected = [source_id for source_id in requested if source_id not in source_ids]
    if rejected:
        raise ProductionManualRecurringError(
            "SOURCE_NOT_IN_PHASE7D4C_COHORT: " + ",".join(rejected)
        )
    selected_ids = requested or list(source_ids)
    selected_set = set(selected_ids)
    hints = _collect_source_hints(root=Path(root).resolve(), source_ids=source_ids)

    all_source_fingerprints: list[dict[str, Any]] = []
    selected_sources: list[Phase6ASource] = []
    for source_id in source_ids:
        entry: PortalInventoryEntry = inventory_by_id[source_id]
        hint = hints.get(source_id, {})
        source = Phase6ASource(
            source_id=source_id,
            source_row=entry.source_row,
            display_name=entry.display_name,
            listing_url=entry.listing_url,
            detected_platform=str(hint.get("detected_platform") or "unknown"),
            resolved_route_url=(
                str(hint.get("resolved_route_url") or "").strip() or None
            ),
            bounded_extracted_jobs=int(hint.get("bounded_extracted_jobs") or 0),
            evidence_run_id=(
                str(hint.get("evidence_run_id") or "").strip() or None
            ),
        )
        all_source_fingerprints.append(source.model_dump(mode="json"))
        if source_id in selected_set:
            selected_sources.append(source)

    if [source.source_id for source in selected_sources] != [
        source_id for source_id in source_ids if source_id in selected_set
    ]:
        raise ProductionManualRecurringError("Selected source ordering is unstable")
    cohort_sha256 = _canonical_sha256(
        {
            "policy_sha256": policy.get("policy_sha256"),
            "cohort_policy_sha256": policy.get("cohort_policy_sha256"),
            "sources": all_source_fingerprints,
        }
    )
    generated = generated_at or _utc_now()
    return Phase6AIngestionPlan(
        plan_id="phase7d4c_manual_" + cohort_sha256[:20],
        generated_at=generated,
        cohort_sha256=cohort_sha256,
        cohort_source_count=len(source_ids),
        selected_source_count=len(selected_sources),
        deferred_source_count=max(0, len(inventory) - len(source_ids)),
        selected_source_ids=[source.source_id for source in selected_sources],
        sources=selected_sources,
        controls={
            "execution_mode": "plan_only",
            "max_source_concurrency": 1,
            "source_timeout_seconds": int(
                policy["execution"]["source_timeout_seconds"]
            ),
            "production_writes_enabled": False,
            "lifecycle_reconciliation_enabled": False,
            "deactivation_enabled": False,
            "automatic_scheduler_enabled": False,
        },
    )


def build_phase7d4c_runner_config(
    policy: Mapping[str, Any],
) -> Phase6ERunnerConfig:
    execution = policy["execution"]
    return Phase6ERunnerConfig(
        execution_mode="write",
        max_source_concurrency=1,
        max_attempts=int(execution["max_attempts"]),
        retry_backoff_seconds=1,
        catalog_mode="complete_catalog",
        max_jobs=None,
        max_pages=int(execution["max_pages_per_source"]),
        detail_concurrency=1,
        detail_retry_attempts=int(execution["detail_retry_attempts"]),
        requests_per_minute=int(execution["requests_per_minute"]),
        source_timeout_seconds=int(execution["source_timeout_seconds"]),
        acquisition_timeout_seconds=float(
            execution["acquisition_timeout_seconds"]
        ),
        allow_llm_fallback=bool(execution["allow_llm_fallback"]),
        incremental_rescrape=True,
        normalized_job_writes_enabled=True,
        lifecycle_reconciliation_enabled=False,
        deactivation_enabled=False,
    )


class MongoRecurringLease:
    """Single renewable Mongo lease that prevents overlapping manual cycles."""

    def __init__(
        self,
        database: Database,
        *,
        lease_key: str,
        lease_seconds: int,
        token: str | None = None,
    ) -> None:
        self.collection = database["production_recurring_leases"]
        self.lease_key = lease_key
        self.lease_seconds = max(60, int(lease_seconds))
        self.token = token or uuid.uuid4().hex

    def acquire(self) -> None:
        now = _utc_now()
        try:
            document = self.collection.find_one_and_update(
                {
                    "_id": self.lease_key,
                    "$or": [
                        {"lease_until": {"$lte": now}},
                        {"lease_until": {"$exists": False}},
                        {"lease_token": self.token},
                    ],
                },
                {
                    "$set": {
                        "lease_key": self.lease_key,
                        "lease_token": self.token,
                        "lease_until": now + timedelta(seconds=self.lease_seconds),
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError as exc:
            raise ProductionManualRecurringError(
                "Another Phase 7D4C manual recurring cycle holds the lease"
            ) from exc
        if not document or document.get("lease_token") != self.token:
            raise ProductionManualRecurringError(
                "Another Phase 7D4C manual recurring cycle holds the lease"
            )

    def renew(self) -> None:
        now = _utc_now()
        result = self.collection.update_one(
            {"_id": self.lease_key, "lease_token": self.token},
            {
                "$set": {
                    "lease_until": now + timedelta(seconds=self.lease_seconds),
                    "updated_at": now,
                }
            },
        )
        if int(result.matched_count) != 1:
            raise ProductionManualRecurringError(
                "Phase 7D4C recurring lease was lost during execution"
            )

    def release(self) -> None:
        now = _utc_now()
        self.collection.update_one(
            {"_id": self.lease_key, "lease_token": self.token},
            {
                "$set": {
                    "lease_until": now,
                    "released_at": now,
                    "updated_at": now,
                },
                "$unset": {"lease_token": ""},
            },
        )


def ensure_manual_cycle_is_due(
    database: Database,
    *,
    expected_source_count: int,
    force: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refuse an accidental early full-cohort rerun unless --force is explicit."""
    if force or expected_source_count != 22:
        return {"due": True, "forced": bool(force), "last_cycle_id": None}
    current = _mongo_datetime_as_utc(now or _utc_now()) or _utc_now()
    last = database["production_recurring_cycles"].find_one(
        {
            "cohort_name": PHASE_7D4C_COHORT_NAME,
            "selected_source_count": 22,
            "status": {
                "$in": [
                    "completed",
                    "completed_with_partial",
                    "completed_with_failures",
                ]
            },
        },
        sort=[("completed_at", -1)],
    )
    if not last:
        return {"due": True, "forced": False, "last_cycle_id": None}
    next_due = _mongo_datetime_as_utc(last.get("next_due_at"))
    if next_due is not None and next_due > current:
        raise ProductionManualRecurringError(
            "The 22-source recurring cycle is not due until "
            + next_due.astimezone(timezone.utc).isoformat()
            + "; use --force only for an intentional early run"
        )
    return {
        "due": True,
        "forced": False,
        "last_cycle_id": str(last.get("cycle_id") or ""),
    }


def write_phase7d4c_report(path: Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


def _snapshot_document(
    *,
    cycle_id: str,
    source_id: str,
    source_run_id: str,
    result: Phase6ESourceResult,
) -> dict[str, Any]:
    urls = canonical_job_urls(result.discovered_job_urls)
    return {
        "cycle_id": cycle_id,
        "source_id": source_id,
        "source_run_id": source_run_id,
        "captured_at": _utc_now(),
        "catalog_mode": result.catalog_mode,
        "discovery_complete": result.discovery_complete,
        "catalog_complete": result.catalog_complete,
        "reconciliation_safe": result.reconciliation_safe,
        "reported_discovered_count": result.discovered_count,
        "canonical_url_count": len(urls),
        "canonical_urls": urls,
        "snapshot_sha256": _canonical_sha256(urls),
    }


def process_changed_only_downstream(
    database: Database,
    *,
    source_id: str,
    run_session_id: str,
    changed_job_ids: Sequence[str],
    deactivated_job_ids: Sequence[str],
) -> dict[str, Any]:
    """Update only changed active jobs and remove newly inactive job vectors."""
    changed_ids = sorted(
        {str(value).strip() for value in changed_job_ids if str(value).strip()}
    )
    inactive_ids = sorted(
        {
            str(value).strip()
            for value in deactivated_job_ids
            if str(value).strip()
        }
    )
    active_ids = sorted(set(changed_ids).difference(inactive_ids))
    if not active_ids and not inactive_ids:
        return {
            "status": "skipped_no_changes",
            "changed_job_count": 0,
            "deactivated_job_count": 0,
            "job_towers_upserted": 0,
            "qdrant_indexed": 0,
            "qdrant_deleted": 0,
        }

    warehouse = WarehouseRepository(database)
    active_jobs = warehouse.active_jobs(job_ids=active_ids) if active_ids else []
    for job in active_jobs:
        warehouse.upsert_job_tower(build_job_tower_document(job))

    settings = load_app_settings()
    store = None
    indexed = 0
    if active_jobs:
        store = build_vector_store(settings.vector)
        health = store.healthcheck()
        if not bool(health.get("ok")):
            raise ProductionManualRecurringError(
                f"Qdrant healthcheck failed before changed-only indexing: {health}"
            )
        index_result = index_job_towers(
            repo=warehouse,
            store=store,
            embedder=build_embedder(settings.vector),
            collection_name=settings.vector.jobs_collection,
            recreate=False,
            only_pending=True,
            limit=len(active_jobs),
            job_ids=[str(job["job_id"]) for job in active_jobs],
        )
        indexed = int(index_result.get("indexed_records") or 0)

    point_ids: list[str] = []
    if inactive_ids:
        states = list(
            database["qdrant_index_state"].find(
                {"record_type": "job", "record_id": {"$in": inactive_ids}},
                {"qdrant_point_id": 1, "collection_name_value": 1},
            )
        )
        point_ids = sorted(
            {
                str(row.get("qdrant_point_id") or "").strip()
                for row in states
                if str(row.get("qdrant_point_id") or "").strip()
            }
        )
        if point_ids:
            store = store or build_vector_store(settings.vector)
            health = store.healthcheck()
            if not bool(health.get("ok")):
                raise ProductionManualRecurringError(
                    f"Qdrant healthcheck failed before inactive cleanup: {health}"
                )
            points_by_collection: dict[str, list[str]] = {}
            for row in states:
                point_id = str(row.get("qdrant_point_id") or "").strip()
                if not point_id:
                    continue
                collection_name = str(
                    row.get("collection_name_value")
                    or settings.vector.jobs_collection
                )
                points_by_collection.setdefault(collection_name, []).append(
                    point_id
                )
            for collection_name, collection_point_ids in sorted(
                points_by_collection.items()
            ):
                store.delete_points(
                    collection_name,
                    point_ids=collection_point_ids,
                )
        database["job_tower_records"].delete_many(
            {"job_id": {"$in": inactive_ids}}
        )
        database["qdrant_index_state"].delete_many(
            {"record_type": "job", "record_id": {"$in": inactive_ids}}
        )
        database["candidate_job_matches"].delete_many(
            {"job_id": {"$in": inactive_ids}}
        )
        database["candidate_job_matches_llm_reranked"].delete_many(
            {"job_id": {"$in": inactive_ids}}
        )

    refresh = warehouse.record_recommendation_refresh_requests(
        portal_id=source_id,
        target_id=source_id,
        run_session_id=run_session_id,
        changed_job_ids=sorted(set(changed_ids + inactive_ids)),
    )
    return {
        "status": "completed",
        "changed_job_count": len(changed_ids),
        "deactivated_job_count": len(inactive_ids),
        "job_towers_upserted": len(active_jobs),
        "qdrant_indexed": indexed,
        "qdrant_deleted": len(point_ids),
        "recommendation_refresh": refresh,
    }


SourceRunCallable = Callable[
    [Phase6ASource, str], Awaitable[Phase6ESourceResult]
]
DownstreamCallable = Callable[..., dict[str, Any]]
ProgressCallback = Callable[[dict[str, Any], int, int], None]


class Phase7D4CManualRecurringRunner:
    """Run independent source scrapes with checkpoints and safe lifecycle updates."""

    def __init__(
        self,
        *,
        root: Path,
        output_dir: Path,
        database: Database,
        plan: Phase6AIngestionPlan,
        policy: Mapping[str, Any],
        source_run_callable: SourceRunCallable | None = None,
        downstream_callable: DownstreamCallable | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.database = database
        self.plan = plan
        self.policy = dict(policy)
        self.source_run_callable = source_run_callable
        self.downstream_callable = (
            downstream_callable or process_changed_only_downstream
        )
        self.warehouse = WarehouseRepository(database)

    async def _execute_source(
        self,
        source: Phase6ASource,
        source_run_id: str,
    ) -> Phase6ESourceResult:
        if self.source_run_callable is not None:
            return await self.source_run_callable(source, source_run_id)

        def plan_incremental_details(
            discovered_urls: list[str],
        ) -> tuple[list[str], dict[str, Any]]:
            detail_plan = self.warehouse.plan_detail_rescrape(
                target_id=source.source_id,
                run_session_id=source_run_id,
                discovered_urls=discovered_urls,
                force_detail_refresh=False,
                deep_refresh_days=int(
                    self.policy["execution"]["deep_refresh_days"]
                ),
            )
            return list(detail_plan.get("urls_to_extract") or []), {
                **detail_plan,
                "status": "planned",
            }

        executor = CertificationPhase6ESourceExecutor(
            root=self.root,
            output_dir=self.output_dir / "certification",
            options=build_phase7d4c_runner_config(
                self.policy
            ).certification_options(),
            detail_planner=plan_incremental_details,
        )
        runner = Phase6EProductionRunner(
            plan=self.plan,
            db=self.database,
            executor=executor,
            config=build_phase7d4c_runner_config(self.policy),
        )
        manifest = await runner.run(
            requested_source_ids=[source.source_id],
            run_id=source_run_id,
        )
        if len(manifest.source_results) != 1:
            raise ProductionManualRecurringError(
                f"Source {source.source_id} did not return one terminal result"
            )
        return manifest.source_results[0]

    def _start_or_resume_cycle(
        self,
        *,
        cycle_id: str | None,
    ) -> tuple[str, set[str]]:
        cycles = self.database["production_recurring_cycles"]
        checkpoints = self.database["production_recurring_source_checkpoints"]
        now = _utc_now()
        if cycle_id:
            existing = cycles.find_one({"cycle_id": cycle_id})
            if not existing:
                raise ProductionManualRecurringError(
                    f"Recurring cycle does not exist: {cycle_id}"
                )
            if list(existing.get("selected_source_ids") or []) != list(
                self.plan.selected_source_ids
            ):
                raise ProductionManualRecurringError(
                    "Resume source scope differs from the stored cycle"
                )
            if existing.get("cohort_sha256") != self.plan.cohort_sha256:
                raise ProductionManualRecurringError(
                    "Resume inventory/cohort fingerprint has changed"
                )
            cycles.update_one(
                {"cycle_id": cycle_id},
                {"$set": {"status": "running", "resumed_at": now, "updated_at": now}},
            )
            terminal = {
                str(row.get("source_id") or "")
                for row in checkpoints.find(
                    {
                        "cycle_id": cycle_id,
                        "status": {"$in": sorted(_TERMINAL_CHECKPOINT_STATUSES)},
                    },
                    {"source_id": 1},
                )
            }
            return cycle_id, terminal

        current_id = (
            "phase7d4c_manual_"
            + now.strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:10]
        )
        cycles.insert_one(
            {
                "_id": current_id,
                "cycle_id": current_id,
                "contract_version": PHASE_7D4C_MANUAL_CONTRACT_VERSION,
                "phase": "7D4C",
                "cohort_name": PHASE_7D4C_COHORT_NAME,
                "cohort_sha256": self.plan.cohort_sha256,
                "policy_sha256": self.policy["policy_sha256"],
                "plan_id": self.plan.plan_id,
                "status": "running",
                "selected_source_ids": list(self.plan.selected_source_ids),
                "selected_source_count": len(self.plan.selected_source_ids),
                "started_at": now,
                "created_at": now,
                "updated_at": now,
                "controls": {
                    "automatic_scheduler_enabled": False,
                    "manual_command_required": True,
                    "lifecycle_reconciliation_enabled": True,
                    "deactivation_after_complete_misses": 2,
                    "failed_or_partial_runs_increment_missing_count": False,
                    "physical_job_deletion_enabled": False,
                    "changed_only_downstream_processing": True,
                    "incremental_detail_rescrape": True,
                    "deep_refresh_days": int(
                        self.policy["execution"]["deep_refresh_days"]
                    ),
                },
            }
        )
        return current_id, set()

    async def run(
        self,
        *,
        resume_cycle_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        init_indexes(self.database)
        lease = MongoRecurringLease(
            self.database,
            lease_key=PHASE_7D4C_COHORT_NAME,
            lease_seconds=int(self.policy["execution"]["lease_seconds"]),
        )
        lease.acquire()
        try:
            cycle_id, already_terminal = self._start_or_resume_cycle(
                cycle_id=resume_cycle_id
            )
        except Exception:
            lease.release()
            raise
        checkpoints = self.database["production_recurring_source_checkpoints"]
        snapshots = self.database["production_recurring_source_snapshots"]
        source_by_id = {source.source_id: source for source in self.plan.sources}
        total = len(self.plan.selected_source_ids)

        try:
            for position, source_id in enumerate(
                self.plan.selected_source_ids,
                start=1,
            ):
                if source_id in already_terminal:
                    checkpoint = checkpoints.find_one(
                        {"cycle_id": cycle_id, "source_id": source_id}
                    ) or {}
                    if on_progress is not None:
                        on_progress(dict(checkpoint), position, total)
                    continue
                lease.renew()
                prior_checkpoint = checkpoints.find_one(
                    {"cycle_id": cycle_id, "source_id": source_id}
                ) or {}
                if prior_checkpoint.get("status") in {
                    "downstream_pending",
                    "failed_downstream",
                }:
                    try:
                        downstream = self.downstream_callable(
                            self.database,
                            source_id=source_id,
                            run_session_id=str(
                                prior_checkpoint.get("source_run_id") or ""
                            ),
                            changed_job_ids=list(
                                prior_checkpoint.get("changed_job_ids") or []
                            ),
                            deactivated_job_ids=list(
                                prior_checkpoint.get("deactivated_job_ids") or []
                            ),
                        )
                        resumed_payload = {
                            "status": str(
                                prior_checkpoint.get("desired_terminal_status")
                                or "partial"
                            ),
                            "downstream": downstream,
                            "completed_at": _utc_now(),
                            "updated_at": _utc_now(),
                        }
                        checkpoints.update_one(
                            {"cycle_id": cycle_id, "source_id": source_id},
                            {
                                "$set": resumed_payload,
                                "$unset": {
                                    "error_type": "",
                                    "error_message": "",
                                },
                            },
                        )
                        progress_payload = dict(prior_checkpoint)
                        progress_payload.update(resumed_payload)
                    except Exception as exc:
                        resumed_payload = {
                            "status": "failed_downstream",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:4000],
                            "updated_at": _utc_now(),
                        }
                        checkpoints.update_one(
                            {"cycle_id": cycle_id, "source_id": source_id},
                            {"$set": resumed_payload},
                        )
                        progress_payload = dict(prior_checkpoint)
                        progress_payload.update(resumed_payload)
                    if on_progress is not None:
                        on_progress(progress_payload, position, total)
                    continue
                now = _utc_now()
                try:
                    prior_attempt_number = int(
                        prior_checkpoint.get("attempt_number") or 0
                    )
                except (TypeError, ValueError) as exc:
                    raise ProductionManualRecurringError(
                        "Stored recurring source attempt_number is invalid for "
                        + source_id
                    ) from exc
                if prior_attempt_number < 0:
                    raise ProductionManualRecurringError(
                        "Stored recurring source attempt_number is invalid for "
                        + source_id
                    )
                attempt_number = prior_attempt_number + 1
                source_run_id = _source_attempt_run_id(
                    cycle_id=cycle_id,
                    position=position,
                    source_id=source_id,
                    attempt_number=attempt_number,
                )
                attempt_run_ids = [
                    str(value)
                    for value in prior_checkpoint.get("attempt_run_ids") or []
                    if str(value).strip()
                ]
                prior_source_run_id = str(
                    prior_checkpoint.get("source_run_id") or ""
                ).strip()
                if (
                    prior_source_run_id
                    and prior_source_run_id not in attempt_run_ids
                ):
                    attempt_run_ids.append(prior_source_run_id)
                if source_run_id in attempt_run_ids:
                    raise ProductionManualRecurringError(
                        "Recurring source attempt generated a duplicate run id: "
                        + source_run_id
                    )
                attempt_run_ids.append(source_run_id)
                checkpoints.update_one(
                    {"cycle_id": cycle_id, "source_id": source_id},
                    {
                        "$set": {
                            "status": "running",
                            "attempt_number": attempt_number,
                            "source_run_id": source_run_id,
                            "attempt_run_ids": attempt_run_ids,
                            "started_at": now,
                            "updated_at": now,
                        },
                        "$setOnInsert": {
                            "_id": f"{cycle_id}:{source_id}",
                            "cycle_id": cycle_id,
                            "source_id": source_id,
                            "created_at": now,
                        },
                        "$unset": {
                            "result": "",
                            "snapshot": "",
                            "lifecycle": "",
                            "downstream": "",
                            "changed_job_ids": "",
                            "deactivated_job_ids": "",
                            "desired_terminal_status": "",
                            "completed_at": "",
                            "error_type": "",
                            "error_message": "",
                        },
                    },
                    upsert=True,
                )
                checkpoint_payload: dict[str, Any]
                try:
                    result = await self._execute_source(
                        source_by_id[source_id],
                        source_run_id,
                    )
                    snapshot = _snapshot_document(
                        cycle_id=cycle_id,
                        source_id=source_id,
                        source_run_id=source_run_id,
                        result=result,
                    )
                    snapshots.update_one(
                        {"cycle_id": cycle_id, "source_id": source_id},
                        {
                            "$set": snapshot,
                            "$setOnInsert": {
                                "_id": f"{cycle_id}:{source_id}",
                                "created_at": now,
                            },
                        },
                        upsert=True,
                    )

                    lifecycle: dict[str, Any] = {
                        "status": "skipped_incomplete_or_failed_snapshot",
                        "missing_marked": 0,
                        "deactivated": 0,
                        "deactivated_job_ids": [],
                        "reactivated_job_ids": [],
                    }
                    if result.reconciliation_safe:
                        lifecycle = self.warehouse.reconcile_missing_jobs_after_discovery(
                            target_id=source_id,
                            run_session_id=source_run_id,
                            discovered_urls=list(result.discovered_job_urls),
                            deactivate_after_misses=int(
                                self.policy["lifecycle"][
                                    "deactivate_after_complete_misses"
                                ]
                            ),
                            min_discovery_coverage_ratio=float(
                                self.policy["lifecycle"][
                                    "min_discovery_coverage_ratio"
                                ]
                            ),
                            allow_empty_discovery=False,
                        )

                    changed_ids = sorted(
                        set(result.changed_job_ids)
                        | set(lifecycle.get("reactivated_job_ids") or [])
                    )
                    deactivated_ids = list(
                        lifecycle.get("deactivated_job_ids") or []
                    )
                    if result.reconciliation_safe:
                        checkpoint_status = (
                            "complete"
                            if lifecycle.get("status") == "completed"
                            and result.status == "success"
                            else "complete_guarded"
                        )
                    elif result.accepted_count > 0:
                        checkpoint_status = "partial"
                    else:
                        checkpoint_status = "failed"
                    pending_payload = {
                        "status": "downstream_pending",
                        "source_run_id": source_run_id,
                        "desired_terminal_status": checkpoint_status,
                        "result": result.model_dump(mode="json"),
                        "snapshot": {
                            "snapshot_sha256": snapshot["snapshot_sha256"],
                            "canonical_url_count": snapshot[
                                "canonical_url_count"
                            ],
                            "reconciliation_safe": snapshot[
                                "reconciliation_safe"
                            ],
                        },
                        "lifecycle": lifecycle,
                        "changed_job_ids": changed_ids,
                        "deactivated_job_ids": deactivated_ids,
                        "updated_at": _utc_now(),
                    }
                    checkpoints.update_one(
                        {"cycle_id": cycle_id, "source_id": source_id},
                        {"$set": pending_payload},
                    )
                    downstream = self.downstream_callable(
                        self.database,
                        source_id=source_id,
                        run_session_id=source_run_id,
                        changed_job_ids=changed_ids,
                        deactivated_job_ids=deactivated_ids,
                    )
                    checkpoint_payload = {
                        "status": checkpoint_status,
                        "source_run_id": source_run_id,
                        "result": result.model_dump(mode="json"),
                        "snapshot": {
                            "snapshot_sha256": snapshot["snapshot_sha256"],
                            "canonical_url_count": snapshot[
                                "canonical_url_count"
                            ],
                            "reconciliation_safe": snapshot[
                                "reconciliation_safe"
                            ],
                        },
                        "lifecycle": lifecycle,
                        "downstream": downstream,
                        "completed_at": _utc_now(),
                        "updated_at": _utc_now(),
                    }
                except Exception as exc:
                    persisted = checkpoints.find_one(
                        {"cycle_id": cycle_id, "source_id": source_id}
                    ) or {}
                    downstream_pending = persisted.get("status") == "downstream_pending"
                    checkpoint_payload = {
                        "status": (
                            "failed_downstream" if downstream_pending else "failed"
                        ),
                        "source_run_id": source_run_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:4000],
                        "completed_at": _utc_now(),
                        "updated_at": _utc_now(),
                    }
                checkpoints.update_one(
                    {"cycle_id": cycle_id, "source_id": source_id},
                    {"$set": checkpoint_payload},
                )
                checkpoint_payload.update(
                    {"cycle_id": cycle_id, "source_id": source_id}
                )
                if on_progress is not None:
                    on_progress(checkpoint_payload, position, total)
        finally:
            lease.release()

        checkpoint_rows = list(
            checkpoints.find(
                {
                    "cycle_id": cycle_id,
                    "source_id": {"$in": list(self.plan.selected_source_ids)},
                }
            )
        )
        by_source = {
            str(row.get("source_id") or ""): row for row in checkpoint_rows
        }
        ordered = [
            by_source.get(source_id, {"source_id": source_id, "status": "failed"})
            for source_id in self.plan.selected_source_ids
        ]
        status_counts: dict[str, int] = {}
        for row in ordered:
            status = str(row.get("status") or "failed")
            status_counts[status] = status_counts.get(status, 0) + 1
        failures = sum(
            count
            for status, count in status_counts.items()
            if status not in _TERMINAL_CHECKPOINT_STATUSES
        )
        partials = status_counts.get("partial", 0) + status_counts.get(
            "complete_guarded", 0
        )
        if failures:
            cycle_status = "completed_with_failures"
            delay_hours = int(self.policy["failure_retry_hours"])
        elif partials:
            cycle_status = "completed_with_partial"
            delay_hours = int(self.policy["cadence_hours"])
        else:
            cycle_status = "completed"
            delay_hours = int(self.policy["cadence_hours"])
        completed_at = _utc_now()
        next_due_at = completed_at + timedelta(hours=delay_hours)

        counters = {
            key: sum(
                int((row.get("result") or {}).get(key) or 0)
                for row in ordered
            )
            for key in (
                "discovered_count",
                "attempted_count",
                "extracted_count",
                "accepted_count",
                "inserted_count",
                "updated_count",
                "unchanged_count",
                "reactivated_count",
            )
        }
        counters["missing_marked"] = sum(
            int((row.get("lifecycle") or {}).get("missing_marked") or 0)
            for row in ordered
        )
        counters["deactivated"] = sum(
            int((row.get("lifecycle") or {}).get("deactivated") or 0)
            for row in ordered
        )
        report = {
            "contract_version": PHASE_7D4C_MANUAL_CONTRACT_VERSION,
            "phase": "7D4C",
            "mode": "manual_recurring_cycle",
            "cycle_id": cycle_id,
            "status": cycle_status,
            "completed_at": completed_at.isoformat(),
            "next_due_at": next_due_at.isoformat(),
            "cohort_name": PHASE_7D4C_COHORT_NAME,
            "cohort_sha256": self.plan.cohort_sha256,
            "policy_sha256": self.policy["policy_sha256"],
            "selected_source_ids": list(self.plan.selected_source_ids),
            "selected_source_count": len(self.plan.selected_source_ids),
            "status_counts": status_counts,
            "counters": counters,
            "source_checkpoints": [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"_id", "created_at", "updated_at"}
                }
                for row in ordered
            ],
            "controls": {
                "automatic_scheduler_enabled": False,
                "manual_command_required": True,
                "cadence_hours": int(self.policy["cadence_hours"]),
                "source_concurrency": 1,
                "gpu_llm_concurrency": 1,
                "complete_snapshot_required_for_reconciliation": True,
                "failed_or_partial_runs_increment_missing_count": False,
                "deactivate_after_complete_misses": 2,
                "physical_job_deletion_enabled": False,
                "changed_only_downstream_processing": True,
                "incremental_detail_rescrape": True,
                "deep_refresh_days": int(
                    self.policy["execution"]["deep_refresh_days"]
                ),
            },
        }
        self.database["production_recurring_cycles"].update_one(
            {"cycle_id": cycle_id},
            {
                "$set": {
                    "status": cycle_status,
                    "status_counts": status_counts,
                    "counters": counters,
                    "completed_at": completed_at,
                    "next_due_at": next_due_at,
                    "updated_at": completed_at,
                    "report_sha256": _canonical_sha256(report),
                }
            },
        )
        return report
