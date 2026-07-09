from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .repository import ControlRepository


PORTAL_ENTITY_TYPE = "job_portal"


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _schedule_to_interval_minutes(schedule_expression: str | None, explicit_minutes: int | None) -> int | None:
    if explicit_minutes is not None:
        return _clamp_int(explicit_minutes, 1440, 15, 10080)
    value = str(schedule_expression or '').strip().lower()
    if not value:
        return None
    presets = {
        'hourly': 60,
        'daily': 1440,
        'weekly': 10080,
        'every_6h': 360,
        'every_12h': 720,
        'every_24h': 1440,
    }
    if value in presets:
        return presets[value]
    match = re.match(r'^every[_\s-]*(\d+)[_\s-]*(m|min|mins|minutes|h|hr|hrs|hours|d|day|days)$', value)
    if match:
        number = int(match.group(1))
        unit = match.group(2)
        multiplier = 1 if unit.startswith('m') else 60 if unit.startswith('h') else 1440
        return _clamp_int(number * multiplier, 1440, 15, 10080)
    return None


def _next_run_at(enabled: bool, interval_minutes: int | None, from_time: datetime | None = None) -> datetime | None:
    if not enabled or not interval_minutes:
        return None
    base = from_time or _utc_now()
    return base + timedelta(minutes=_clamp_int(interval_minutes, 1440, 15, 10080))


def slugify_portal_key(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    value = value.strip("-")
    return value[:80] or "job-portal"


class JobPortalRepository:
    """Postgres repository for durable portal configuration and run control.

    It deliberately reuses the existing generic control-plane operational tables
    rather than creating portal-specific task/run/audit duplicates.
    """

    def __init__(self, session: Session):
        self.session = session
        self.control = ControlRepository(session)

    def create_portal(
        self,
        *,
        organization_id: str,
        actor: dict[str, Any],
        display_name: str,
        listing_url: str,
        normalized_host: str,
        allowed_hosts: list[str],
        max_pages_per_run: int,
        max_jobs_per_run: int,
        request_rate_limit_per_minute: int,
        crawl_timeout_seconds: int,
        schedule_expression: str | None,
        scheduler_enabled: bool = False,
        refresh_interval_minutes: int | None = None,
        deactivate_after_misses: int = 2,
        min_discovery_coverage_ratio: float = 0.25,
        max_consecutive_failures_before_pause: int = 5,
        detail_retry_attempts: int = 2,
    ) -> dict[str, Any]:
        base_key = slugify_portal_key(display_name)
        portal_key = self._next_portal_key(organization_id, base_key)
        portal_id = str(uuid.uuid4())
        target_id = f"portal_{portal_id.replace('-', '')}"
        actor_id = actor.get("id")
        interval_minutes = _schedule_to_interval_minutes(schedule_expression, refresh_interval_minutes)
        scheduler_enabled = bool(scheduler_enabled and interval_minutes)
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.job_portals (
                    id, organization_id, portal_key, target_id, display_name,
                    listing_url, canonical_listing_url, normalized_host, allowed_hosts,
                    status, last_run_status, is_active,
                    max_pages_per_run, max_jobs_per_run, request_rate_limit_per_minute,
                    crawl_timeout_seconds, schedule_expression, scheduler_enabled, refresh_interval_minutes,
                    deactivate_after_misses, min_discovery_coverage_ratio,
                    max_consecutive_failures_before_pause, detail_retry_attempts,
                    next_run_at, created_by, modified_by, metadata
                ) VALUES (
                    :id, :organization_id, :portal_key, :target_id, :display_name,
                    :listing_url, :canonical_listing_url, :normalized_host, CAST(:allowed_hosts AS jsonb),
                    'probing', 'queued', FALSE,
                    :max_pages_per_run, :max_jobs_per_run, :request_rate_limit_per_minute,
                    :crawl_timeout_seconds, :schedule_expression, :scheduler_enabled, :refresh_interval_minutes,
                    :deactivate_after_misses, :min_discovery_coverage_ratio,
                    :max_consecutive_failures_before_pause, :detail_retry_attempts,
                    :next_run_at, :actor_id, :actor_id, CAST(:metadata AS jsonb)
                )
                RETURNING *
                """
            ),
            {
                "id": portal_id,
                "organization_id": organization_id,
                "portal_key": portal_key,
                "target_id": target_id,
                "display_name": display_name.strip(),
                "listing_url": listing_url,
                "canonical_listing_url": listing_url,
                "normalized_host": normalized_host,
                "allowed_hosts": _json(allowed_hosts),
                "max_pages_per_run": max_pages_per_run,
                "max_jobs_per_run": max_jobs_per_run,
                "request_rate_limit_per_minute": request_rate_limit_per_minute,
                "crawl_timeout_seconds": crawl_timeout_seconds,
                "schedule_expression": schedule_expression.strip() if schedule_expression else None,
                "scheduler_enabled": scheduler_enabled,
                "refresh_interval_minutes": interval_minutes,
                "deactivate_after_misses": _clamp_int(deactivate_after_misses, 2, 1, 10),
                "min_discovery_coverage_ratio": _clamp_float(min_discovery_coverage_ratio, 0.25, 0.05, 1.0),
                "max_consecutive_failures_before_pause": _clamp_int(max_consecutive_failures_before_pause, 5, 1, 20),
                "detail_retry_attempts": _clamp_int(detail_retry_attempts, 2, 0, 5),
                "next_run_at": _next_run_at(scheduler_enabled, interval_minutes),
                "actor_id": actor_id,
                "metadata": _json({"created_via": "admin_portal_ui"}),
            },
        ).mappings().one()
        portal = dict(row)
        self.control.add_audit_event(
            organization_id=organization_id,
            actor_app_user_id=str(actor_id) if actor_id else None,
            actor_keycloak_user_id=str(actor.get("login_keycloak_user_id") or actor.get("keycloak_user_id") or ""),
            event_type="portal.created",
            entity_type=PORTAL_ENTITY_TYPE,
            entity_id=portal_id,
            after_payload=self.public_portal_payload(portal),
        )
        return portal

    def _next_portal_key(self, organization_id: str, base_key: str) -> str:
        candidate = base_key
        number = 2
        while self.session.execute(
            text("SELECT 1 FROM job_miner_control.job_portals WHERE organization_id = :org AND portal_key = :key"),
            {"org": organization_id, "key": candidate},
        ).scalar_one_or_none():
            suffix = f"-{number}"
            candidate = f"{base_key[: max(1, 80 - len(suffix))]}{suffix}"
            number += 1
        return candidate

    def get_portal(self, *, portal_id: str, organization_id: str | None, platform_admin: bool = False) -> dict[str, Any] | None:
        clauses = ["id = :portal_id"]
        params: dict[str, Any] = {"portal_id": portal_id}
        if not platform_admin:
            clauses.append("organization_id = :organization_id")
            params["organization_id"] = organization_id
        row = self.session.execute(
            text(f"SELECT * FROM job_miner_control.job_portals WHERE {' AND '.join(clauses)} LIMIT 1"),
            params,
        ).mappings().first()
        return dict(row) if row else None

    def list_portals(self, *, organization_id: str | None, platform_admin: bool, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        params: dict[str, Any] = {"limit": limit}
        where = ""
        if not platform_admin:
            where = "WHERE p.organization_id = :organization_id"
            params["organization_id"] = organization_id
        rows = self.session.execute(
            text(
                f"""
                SELECT p.*,
                       latest.run_session_id AS latest_run_session_id,
                       latest.status AS latest_pipeline_status,
                       latest.completed_on AS latest_pipeline_completed_on,
                       latest.metrics AS latest_pipeline_metrics
                FROM job_miner_control.job_portals p
                LEFT JOIN LATERAL (
                    SELECT run_session_id, status, completed_on, metrics
                    FROM job_miner_control.pipeline_runs
                    WHERE portal_id = p.id
                    ORDER BY created_on DESC
                    LIMIT 1
                ) latest ON TRUE
                {where}
                ORDER BY p.modified_on DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [dict(row) for row in rows]

    def update_portal(
        self,
        *,
        portal: dict[str, Any],
        actor: dict[str, Any],
        display_name: str | None = None,
        listing_url: str | None = None,
        normalized_host: str | None = None,
        allowed_hosts: list[str] | None = None,
        max_pages_per_run: int | None = None,
        max_jobs_per_run: int | None = None,
        request_rate_limit_per_minute: int | None = None,
        crawl_timeout_seconds: int | None = None,
        schedule_expression: str | None = None,
        scheduler_enabled: bool | None = None,
        refresh_interval_minutes: int | None = None,
        deactivate_after_misses: int | None = None,
        min_discovery_coverage_ratio: float | None = None,
        max_consecutive_failures_before_pause: int | None = None,
        detail_retry_attempts: int | None = None,
        expected_configuration_version: int | None = None,
    ) -> dict[str, Any]:
        if expected_configuration_version is not None and int(portal["configuration_version"]) != int(expected_configuration_version):
            raise ValueError("This portal was updated by another administrator. Refresh and try again.")

        before = self.public_portal_payload(portal)
        url_changed = bool(listing_url and listing_url != portal.get("listing_url"))
        interval_minutes = _schedule_to_interval_minutes(
            schedule_expression if schedule_expression is not None else portal.get("schedule_expression"),
            refresh_interval_minutes if refresh_interval_minutes is not None else portal.get("refresh_interval_minutes"),
        )
        effective_scheduler_enabled = bool(
            (scheduler_enabled if scheduler_enabled is not None else portal.get("scheduler_enabled"))
            and interval_minutes
        )
        params = {
            "id": portal["id"],
            "display_name": display_name.strip() if display_name else portal["display_name"],
            "listing_url": listing_url or portal["listing_url"],
            "canonical_listing_url": listing_url or portal.get("canonical_listing_url") or portal["listing_url"],
            "normalized_host": normalized_host or portal["normalized_host"],
            "allowed_hosts": _json(allowed_hosts if allowed_hosts is not None else portal.get("allowed_hosts") or []),
            "max_pages_per_run": max_pages_per_run if max_pages_per_run is not None else portal["max_pages_per_run"],
            "max_jobs_per_run": max_jobs_per_run if max_jobs_per_run is not None else portal["max_jobs_per_run"],
            "request_rate_limit_per_minute": request_rate_limit_per_minute if request_rate_limit_per_minute is not None else portal["request_rate_limit_per_minute"],
            "crawl_timeout_seconds": crawl_timeout_seconds if crawl_timeout_seconds is not None else portal["crawl_timeout_seconds"],
            "schedule_expression": schedule_expression.strip() if schedule_expression is not None and schedule_expression else (portal.get("schedule_expression") if schedule_expression is None else None),
            "scheduler_enabled": effective_scheduler_enabled,
            "refresh_interval_minutes": interval_minutes,
            "deactivate_after_misses": _clamp_int(deactivate_after_misses if deactivate_after_misses is not None else portal.get("deactivate_after_misses"), 2, 1, 10),
            "min_discovery_coverage_ratio": _clamp_float(min_discovery_coverage_ratio if min_discovery_coverage_ratio is not None else portal.get("min_discovery_coverage_ratio"), 0.25, 0.05, 1.0),
            "max_consecutive_failures_before_pause": _clamp_int(max_consecutive_failures_before_pause if max_consecutive_failures_before_pause is not None else portal.get("max_consecutive_failures_before_pause"), 5, 1, 20),
            "detail_retry_attempts": _clamp_int(detail_retry_attempts if detail_retry_attempts is not None else portal.get("detail_retry_attempts"), 2, 0, 5),
            "next_run_at": None if url_changed else _next_run_at(effective_scheduler_enabled, interval_minutes),
            "status": "draft" if url_changed else portal["status"],
            "is_active": False if url_changed else portal["is_active"],
            "source_platform": "unknown" if url_changed else portal.get("source_platform") or "unknown",
            "source_platform_confidence": None if url_changed else portal.get("source_platform_confidence"),
            "crawl_strategy": None if url_changed else portal.get("crawl_strategy"),
            "profile_name": None if url_changed else portal.get("profile_name"),
            "last_run_status": "never" if url_changed else portal.get("last_run_status") or "never",
            "actor_id": actor.get("id"),
        }
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET display_name = :display_name,
                    listing_url = :listing_url,
                    canonical_listing_url = :canonical_listing_url,
                    normalized_host = :normalized_host,
                    allowed_hosts = CAST(:allowed_hosts AS jsonb),
                    max_pages_per_run = :max_pages_per_run,
                    max_jobs_per_run = :max_jobs_per_run,
                    request_rate_limit_per_minute = :request_rate_limit_per_minute,
                    crawl_timeout_seconds = :crawl_timeout_seconds,
                    schedule_expression = :schedule_expression,
                    scheduler_enabled = :scheduler_enabled,
                    refresh_interval_minutes = :refresh_interval_minutes,
                    deactivate_after_misses = :deactivate_after_misses,
                    min_discovery_coverage_ratio = :min_discovery_coverage_ratio,
                    max_consecutive_failures_before_pause = :max_consecutive_failures_before_pause,
                    detail_retry_attempts = :detail_retry_attempts,
                    next_run_at = :next_run_at,
                    status = :status,
                    is_active = :is_active,
                    source_platform = :source_platform,
                    source_platform_confidence = :source_platform_confidence,
                    crawl_strategy = :crawl_strategy,
                    profile_name = :profile_name,
                    last_run_status = :last_run_status,
                    configuration_version = configuration_version + 1,
                    modified_by = :actor_id
                WHERE id = :id
                RETURNING *
                """
            ),
            params,
        ).mappings().one()
        updated = dict(row)
        self.control.add_audit_event(
            organization_id=str(updated["organization_id"]),
            actor_app_user_id=str(actor.get("id")) if actor.get("id") else None,
            actor_keycloak_user_id=str(actor.get("login_keycloak_user_id") or actor.get("keycloak_user_id") or ""),
            event_type="portal.updated",
            entity_type=PORTAL_ENTITY_TYPE,
            entity_id=str(updated["id"]),
            before_payload=before,
            after_payload=self.public_portal_payload(updated),
        )
        return updated

    def set_portal_status(
        self,
        *,
        portal: dict[str, Any],
        actor: dict[str, Any],
        status: str,
        is_active: bool,
        event_type: str,
    ) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET status = :status,
                    is_active = :is_active,
                    lease_token = CASE WHEN :is_active THEN lease_token ELSE NULL END,
                    lease_until = CASE WHEN :is_active THEN lease_until ELSE NULL END,
                    modified_by = :actor_id
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {"status": status, "is_active": is_active, "actor_id": actor.get("id"), "portal_id": portal["id"]},
        ).mappings().one()
        updated = dict(row)
        self.control.add_audit_event(
            organization_id=str(updated["organization_id"]),
            actor_app_user_id=str(actor.get("id")) if actor.get("id") else None,
            actor_keycloak_user_id=str(actor.get("login_keycloak_user_id") or actor.get("keycloak_user_id") or ""),
            event_type=event_type,
            entity_type=PORTAL_ENTITY_TYPE,
            entity_id=str(updated["id"]),
            before_payload=self.public_portal_payload(portal),
            after_payload=self.public_portal_payload(updated),
        )
        return updated

    def prepare_portal_for_probe(self, *, portal: dict[str, Any], actor: dict[str, Any] | None) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET status = 'probing',
                    last_run_status = 'queued',
                    is_active = FALSE,
                    modified_by = :actor_id
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {"portal_id": portal["id"], "actor_id": (actor or {}).get("id")},
        ).mappings().one()
        return dict(row)

    def save_probe_result(self, *, portal_id: str, result: dict[str, Any]) -> dict[str, Any]:
        detected = result.get("detected") or {}
        discovered_urls = int(result.get("discovered_urls") or 0)
        blocked = bool(detected.get("blocked"))
        status = "blocked" if blocked else ("ready_for_test" if discovered_urls > 0 else "needs_review")
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET canonical_listing_url = :canonical_listing_url,
                    source_platform = :source_platform,
                    source_platform_confidence = :confidence,
                    crawl_strategy = :crawl_strategy,
                    profile_name = :profile_name,
                    status = :status,
                    last_run_status = 'completed',
                    last_successful_run_on = NOW(),
                    failure_streak = 0,
                    metadata = metadata || CAST(:metadata AS jsonb),
                    modified_on = NOW()
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {
                "portal_id": portal_id,
                "canonical_listing_url": result.get("final_url"),
                "source_platform": str(detected.get("source_platform") or "unknown"),
                "confidence": detected.get("confidence"),
                "crawl_strategy": detected.get("crawl_strategy"),
                "profile_name": detected.get("profile_name"),
                "status": status,
                "metadata": _json({"last_probe": result}),
            },
        ).mappings().one()
        return dict(row)

    def prepare_portal_for_test(self, *, portal: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET status = 'test_scraping', last_run_status = 'queued', modified_by = :actor_id
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {"portal_id": portal["id"], "actor_id": actor.get("id")},
        ).mappings().one()
        return dict(row)

    def mark_portal_run_started(self, portal_id: str) -> None:
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET last_run_status = 'running', last_run_started_on = NOW(), modified_on = NOW()
                WHERE id = :portal_id
                """
            ),
            {"portal_id": portal_id},
        )

    def mark_portal_run_completed(self, *, portal_id: str, status: str, success: bool, result: dict[str, Any]) -> dict[str, Any]:
        lifecycle_status = status if status else None
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET status = COALESCE(:status, status),
                    last_run_status = CASE WHEN :success THEN 'completed' ELSE 'partial' END,
                    last_successful_run_on = CASE WHEN :success THEN NOW() ELSE last_successful_run_on END,
                    failure_streak = CASE WHEN :success THEN 0 ELSE failure_streak END,
                    metadata = metadata || CAST(:result AS jsonb),
                    modified_on = NOW()
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {"portal_id": portal_id, "status": lifecycle_status, "success": success, "result": _json({"last_run_result": result})},
        ).mappings().one()
        return dict(row)

    def mark_portal_run_failed(self, *, portal_id: str, error_message: str) -> None:
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET status = CASE
                        WHEN status IN ('probing', 'test_scraping') THEN 'needs_review'
                        ELSE status
                    END,
                    last_run_status = 'failed',
                    last_failed_run_on = NOW(),
                    failure_streak = failure_streak + 1,
                    metadata = metadata || CAST(:metadata AS jsonb),
                    modified_on = NOW()
                WHERE id = :portal_id
                """
            ),
            {"portal_id": portal_id, "metadata": _json({"last_run_error": error_message[:2000]})},
        )

    def acquire_lease(self, *, portal_id: str, lease_token: str, seconds: int) -> bool:
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET lease_token = CAST(:lease_token AS uuid),
                    lease_until = NOW() + (:lease_seconds * INTERVAL '1 second'),
                    modified_on = NOW()
                WHERE id = :portal_id
                  AND (lease_until IS NULL OR lease_until < NOW() OR lease_token = CAST(:lease_token AS uuid))
                RETURNING id
                """
            ),
            {"portal_id": portal_id, "lease_token": lease_token, "lease_seconds": max(30, int(seconds))},
        ).mappings().first()
        return row is not None

    def release_lease(self, *, portal_id: str, lease_token: str) -> None:
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET lease_token = NULL, lease_until = NULL, modified_on = NOW()
                WHERE id = :portal_id AND lease_token = CAST(:lease_token AS uuid)
                """
            ),
            {"portal_id": portal_id, "lease_token": lease_token},
        )

    def create_portal_pipeline_run(
        self,
        *,
        portal: dict[str, Any],
        pipeline_name: str,
        user: dict[str, Any] | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_session_id = f"{pipeline_name}_{_utc_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:10]}"
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.pipeline_runs (
                    organization_id, portal_id, run_session_id, pipeline_name, status,
                    trigger_type, triggered_by_app_user_id, triggered_by_keycloak_user_id,
                    metadata, created_by, modified_by
                ) VALUES (
                    :organization_id, :portal_id, :run_session_id, :pipeline_name, 'queued',
                    :trigger_type, :app_user_id, :keycloak_user_id,
                    CAST(:metadata AS jsonb), :app_user_id, :app_user_id
                )
                RETURNING *
                """
            ),
            {
                "organization_id": portal["organization_id"],
                "portal_id": portal["id"],
                "run_session_id": run_session_id,
                "pipeline_name": pipeline_name,
                "trigger_type": "api" if user else "scheduler",
                "app_user_id": (user or {}).get("id"),
                "keycloak_user_id": (user or {}).get("login_keycloak_user_id") or (user or {}).get("keycloak_user_id"),
                "metadata": _json(metadata or {}),
            },
        ).mappings().one()
        return dict(row)

    def get_pipeline_run(self, pipeline_run_id: str) -> dict[str, Any] | None:
        row = self.session.execute(
            text("SELECT * FROM job_miner_control.pipeline_runs WHERE id = :id LIMIT 1"),
            {"id": pipeline_run_id},
        ).mappings().first()
        return dict(row) if row else None

    def upsert_step(
        self,
        *,
        pipeline_run_id: str,
        step_key: str,
        step_name: str,
        step_order: int,
        status: str,
        metrics: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.pipeline_run_steps (
                    pipeline_run_id, step_key, step_name, step_order, status,
                    started_on, completed_on, metrics, error_message
                ) VALUES (
                    :pipeline_run_id, :step_key, :step_name, :step_order, :status,
                    CASE WHEN :status = 'running' THEN NOW() ELSE NULL END,
                    CASE WHEN :status IN ('completed', 'failed', 'partial') THEN NOW() ELSE NULL END,
                    CAST(:metrics AS jsonb), :error_message
                )
                ON CONFLICT (pipeline_run_id, step_key) DO UPDATE
                SET status = EXCLUDED.status,
                    started_on = CASE WHEN EXCLUDED.status = 'running' THEN COALESCE(job_miner_control.pipeline_run_steps.started_on, NOW()) ELSE job_miner_control.pipeline_run_steps.started_on END,
                    completed_on = CASE WHEN EXCLUDED.status IN ('completed', 'failed', 'partial') THEN NOW() ELSE job_miner_control.pipeline_run_steps.completed_on END,
                    duration_seconds = CASE WHEN EXCLUDED.status IN ('completed', 'failed', 'partial') THEN EXTRACT(EPOCH FROM (NOW() - COALESCE(job_miner_control.pipeline_run_steps.started_on, job_miner_control.pipeline_run_steps.created_on))) ELSE job_miner_control.pipeline_run_steps.duration_seconds END,
                    metrics = EXCLUDED.metrics,
                    error_message = EXCLUDED.error_message,
                    modified_on = NOW()
                """
            ),
            {
                "pipeline_run_id": pipeline_run_id,
                "step_key": step_key,
                "step_name": step_name,
                "step_order": step_order,
                "status": status,
                "metrics": _json(metrics or {}),
                "error_message": error_message,
            },
        )

    def list_portal_runs(self, *, portal_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT * FROM job_miner_control.pipeline_runs
                WHERE portal_id = :portal_id
                ORDER BY created_on DESC
                LIMIT :limit
                """
            ),
            {"portal_id": portal_id, "limit": max(1, min(limit, 100))},
        ).mappings().all()
        return [dict(row) for row in rows]


    def save_admin_override(
        self,
        *,
        portal: dict[str, Any],
        actor: dict[str, Any],
        profile_name: str,
        source_platform: str | None,
        crawl_strategy: str | None,
        profile_overrides: dict[str, Any],
        notes: str | None = None,
        expected_configuration_version: int | None = None,
    ) -> dict[str, Any]:
        if expected_configuration_version is not None and int(portal["configuration_version"]) != int(expected_configuration_version):
            raise ValueError("This portal was updated by another administrator. Refresh and try again.")
        before = self.public_portal_payload(portal)
        configuration = dict(portal.get("configuration_json") or {})
        configuration["profile_overrides"] = profile_overrides or {}
        configuration.setdefault("admin_overrides", [])
        configuration["admin_overrides"] = [
            *configuration["admin_overrides"][-9:],
            {
                "profile_name": profile_name,
                "source_platform": source_platform or portal.get("source_platform") or "custom_listing",
                "crawl_strategy": crawl_strategy or profile_name,
                "notes": notes,
                "updated_by": str(actor.get("id") or ""),
                "updated_on": _utc_now().isoformat(),
            },
        ]
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET profile_name = :profile_name,
                    source_platform = :source_platform,
                    crawl_strategy = :crawl_strategy,
                    source_platform_confidence = 1.0000,
                    status = CASE WHEN status IN ('blocked', 'paused') THEN status ELSE 'ready_for_test' END,
                    configuration_json = CAST(:configuration AS jsonb),
                    configuration_version = configuration_version + 1,
                    modified_by = :actor_id,
                    metadata = metadata || CAST(:metadata AS jsonb)
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {
                "portal_id": portal["id"],
                "profile_name": profile_name,
                "source_platform": source_platform or portal.get("source_platform") or "custom_listing",
                "crawl_strategy": crawl_strategy or profile_name,
                "configuration": _json(configuration),
                "actor_id": actor.get("id"),
                "metadata": _json({"last_admin_override": {"profile_name": profile_name, "notes": notes, "updated_on": _utc_now().isoformat()}}),
            },
        ).mappings().one()
        updated = dict(row)
        self.control.add_audit_event(
            organization_id=str(updated["organization_id"]),
            actor_app_user_id=str(actor.get("id")) if actor.get("id") else None,
            actor_keycloak_user_id=str(actor.get("login_keycloak_user_id") or actor.get("keycloak_user_id") or ""),
            event_type="portal.override_saved",
            entity_type=PORTAL_ENTITY_TYPE,
            entity_id=str(updated["id"]),
            before_payload=before,
            after_payload=self.public_portal_payload(updated),
        )
        return updated

    def list_due_portals(self, *, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT *
                FROM job_miner_control.job_portals
                WHERE is_active = TRUE
                  AND status = 'active'
                  AND scheduler_enabled = TRUE
                  AND refresh_interval_minutes IS NOT NULL
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= NOW()
                  AND (lease_until IS NULL OR lease_until < NOW())
                ORDER BY next_run_at ASC
                LIMIT :limit
                """
            ),
            {"limit": max(1, min(int(limit), 100))},
        ).mappings().all()
        return [dict(row) for row in rows]

    def set_portal_queued_by_scheduler(self, *, portal_id: str, lease_token: str) -> None:
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET last_run_status = 'queued',
                    lease_token = CAST(:lease_token AS uuid),
                    modified_on = NOW()
                WHERE id = :portal_id
                """
            ),
            {"portal_id": portal_id, "lease_token": lease_token},
        )

    def mark_portal_run_completed(self, *, portal_id: str, status: str, success: bool, result: dict[str, Any]) -> dict[str, Any]:
        lifecycle_status = status if status else None
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET status = COALESCE(:status, status),
                    last_run_status = CASE WHEN :success THEN 'completed' ELSE 'partial' END,
                    last_successful_run_on = CASE WHEN :success THEN NOW() ELSE last_successful_run_on END,
                    failure_streak = CASE WHEN :success THEN 0 ELSE failure_streak END,
                    next_run_at = CASE
                        WHEN :success AND scheduler_enabled = TRUE AND refresh_interval_minutes IS NOT NULL AND status = 'active'
                        THEN NOW() + (refresh_interval_minutes * INTERVAL '1 minute')
                        ELSE next_run_at
                    END,
                    last_health_status = CASE WHEN :success THEN 'healthy' ELSE 'degraded' END,
                    last_health_checked_on = NOW(),
                    metadata = metadata || CAST(:result AS jsonb),
                    modified_on = NOW()
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {"portal_id": portal_id, "status": lifecycle_status, "success": success, "result": _json({"last_run_result": result})},
        ).mappings().one()
        return dict(row)

    def mark_portal_run_failed(self, *, portal_id: str, error_message: str) -> None:
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.job_portals
                SET status = CASE
                        WHEN failure_streak + 1 >= max_consecutive_failures_before_pause AND status = 'active' THEN 'paused'
                        WHEN status IN ('probing', 'test_scraping') THEN 'needs_review'
                        ELSE status
                    END,
                    is_active = CASE
                        WHEN failure_streak + 1 >= max_consecutive_failures_before_pause AND status = 'active' THEN FALSE
                        ELSE is_active
                    END,
                    last_run_status = 'failed',
                    last_failed_run_on = NOW(),
                    failure_streak = failure_streak + 1,
                    next_run_at = CASE
                        WHEN scheduler_enabled = TRUE AND refresh_interval_minutes IS NOT NULL
                             AND failure_streak + 1 < max_consecutive_failures_before_pause
                        THEN NOW() + (LEAST(refresh_interval_minutes * POWER(2, LEAST(failure_streak + 1, 4)), 10080) * INTERVAL '1 minute')
                        ELSE next_run_at
                    END,
                    last_health_status = CASE
                        WHEN failure_streak + 1 >= max_consecutive_failures_before_pause THEN 'auto_paused'
                        ELSE 'failing'
                    END,
                    last_health_checked_on = NOW(),
                    last_alert_on = CASE
                        WHEN failure_streak + 1 >= max_consecutive_failures_before_pause THEN NOW()
                        ELSE last_alert_on
                    END,
                    alert_status = CASE
                        WHEN failure_streak + 1 >= max_consecutive_failures_before_pause THEN 'auto_paused_after_failures'
                        ELSE alert_status
                    END,
                    lease_token = NULL,
                    lease_until = NULL,
                    metadata = metadata || CAST(:metadata AS jsonb),
                    modified_on = NOW()
                WHERE id = :portal_id
                RETURNING *
                """
            ),
            {"portal_id": portal_id, "metadata": _json({"last_run_error": error_message[:2000]})},
        ).mappings().one()
        updated = dict(row)
        if str(updated.get("alert_status") or "") == "auto_paused_after_failures":
            self.control.add_audit_event(
                organization_id=str(updated["organization_id"]),
                actor_app_user_id=None,
                actor_keycloak_user_id=None,
                event_type="portal.auto_paused_after_failures",
                entity_type=PORTAL_ENTITY_TYPE,
                entity_id=str(updated["id"]),
                after_payload={"failure_streak": updated.get("failure_streak"), "error_message": error_message[:2000]},
            )

    def record_file_artifacts(
        self,
        *,
        portal: dict[str, Any],
        pipeline_run_id: str,
        artifacts: list[dict[str, Any]],
    ) -> None:
        for artifact in artifacts or []:
            self.session.execute(
                text(
                    """
                    INSERT INTO job_miner_control.file_artifacts (
                        organization_id, pipeline_run_id, artifact_id, artifact_category, artifact_type,
                        original_file_name, original_relative_path, file_extension, mime_type,
                        size_bytes, sha256, storage_mode, external_uri, metadata
                    ) VALUES (
                        :organization_id, :pipeline_run_id, :artifact_id, :artifact_category, :artifact_type,
                        :original_file_name, :original_relative_path, :file_extension, :mime_type,
                        :size_bytes, :sha256, :storage_mode, :external_uri, CAST(:metadata AS jsonb)
                    )
                    ON CONFLICT (artifact_id) DO UPDATE
                    SET size_bytes = EXCLUDED.size_bytes,
                        sha256 = EXCLUDED.sha256,
                        metadata = EXCLUDED.metadata,
                        modified_on = NOW()
                    """
                ),
                {
                    "organization_id": portal.get("organization_id"),
                    "pipeline_run_id": pipeline_run_id,
                    "artifact_id": artifact.get("artifact_id"),
                    "artifact_category": artifact.get("artifact_category") or "portal_crawl",
                    "artifact_type": artifact.get("artifact_type") or "debug",
                    "original_file_name": artifact.get("file_name"),
                    "original_relative_path": artifact.get("relative_path"),
                    "file_extension": artifact.get("file_extension"),
                    "mime_type": artifact.get("mime_type"),
                    "size_bytes": artifact.get("size_bytes"),
                    "sha256": artifact.get("sha256"),
                    "storage_mode": artifact.get("storage_mode") or "temporary_local",
                    "external_uri": artifact.get("external_uri"),
                    "metadata": _json(artifact.get("metadata") or {}),
                },
            )

    def portal_health_summary(self, *, organization_id: str | None, platform_admin: bool, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        where = ""
        if not platform_admin:
            where = "WHERE p.organization_id = :organization_id"
            params["organization_id"] = organization_id
        rows = self.session.execute(
            text(
                f"""
                SELECT p.id, p.organization_id, p.portal_key, p.target_id, p.display_name,
                       p.status, p.last_run_status, p.is_active, p.scheduler_enabled,
                       p.refresh_interval_minutes, p.next_run_at, p.failure_streak,
                       p.last_health_status, p.last_successful_run_on, p.last_failed_run_on,
                       p.last_alert_on, p.alert_status, p.source_platform, p.profile_name,
                       latest.pipeline_name AS latest_pipeline_name,
                       latest.status AS latest_pipeline_status,
                       latest.duration_seconds AS latest_duration_seconds,
                       latest.metrics AS latest_pipeline_metrics
                FROM job_miner_control.job_portals p
                LEFT JOIN LATERAL (
                    SELECT pipeline_name, status, duration_seconds, metrics
                    FROM job_miner_control.pipeline_runs
                    WHERE portal_id = p.id
                    ORDER BY created_on DESC
                    LIMIT 1
                ) latest ON TRUE
                {where}
                ORDER BY p.modified_on DESC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def public_portal_payload(portal: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id", "organization_id", "portal_key", "target_id", "display_name", "listing_url",
            "canonical_listing_url", "normalized_host", "allowed_hosts", "source_platform",
            "source_platform_confidence", "crawl_strategy", "profile_name", "configuration_json", "configuration_version",
            "status", "last_run_status", "is_active", "failure_streak", "schedule_expression",
            "scheduler_enabled", "refresh_interval_minutes", "next_run_at", "last_run_started_on",
            "last_successful_run_on", "last_failed_run_on", "deactivate_after_misses",
            "min_discovery_coverage_ratio", "max_consecutive_failures_before_pause", "detail_retry_attempts",
            "last_health_status", "last_health_checked_on", "last_alert_on", "alert_status",
            "max_pages_per_run", "max_jobs_per_run", "request_rate_limit_per_minute",
            "crawl_timeout_seconds", "created_on", "modified_on", "metadata",
        )
        return {name: portal.get(name) for name in fields}
