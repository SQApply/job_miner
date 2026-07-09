from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..api.mongo_views import get_candidate_profile, get_job_by_id
from ..common.constants import ApplicationRunStatuses, ApplicationStatuses
from ..control.postgres import postgres_session
from ..control.repository import ControlRepository
from ..observability.correlation import new_uuid
from .graphs import invoke_job_application_graph
from .notifications import ApplicationNotificationService
from .state import JobApplicationState
from .strategies import GenericExternalReviewStrategy, StrategicStaffDirectApplyStrategy, UnsupportedPortalStrategy
from .strategies.base import SubmissionResult

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = {
    ApplicationRunStatuses.SUBMITTED,
    ApplicationRunStatuses.FAILED,
    ApplicationRunStatuses.NEEDS_REVIEW,
    ApplicationRunStatuses.PRECHECK_FAILED,
    ApplicationRunStatuses.BLOCKED_EXTERNAL_LOGIN,
    ApplicationRunStatuses.BLOCKED_CAPTCHA,
    ApplicationRunStatuses.UNSUPPORTED_PORTAL,
    ApplicationRunStatuses.SKIPPED_DUPLICATE,
}

TERMINAL_APPLICATION_STATUSES = {
    ApplicationStatuses.AGENT_APPLIED,
    ApplicationStatuses.APPLIED_MANUALLY,
}

RUN_STATUS_TO_APPLICATION_STATUS = {
    ApplicationRunStatuses.SUBMITTED: ApplicationStatuses.AGENT_APPLIED,
    ApplicationRunStatuses.NEEDS_REVIEW: ApplicationStatuses.AGENT_NEEDS_REVIEW,
    ApplicationRunStatuses.PRECHECK_FAILED: ApplicationStatuses.AGENT_FAILED,
    ApplicationRunStatuses.BLOCKED_EXTERNAL_LOGIN: ApplicationStatuses.AGENT_BLOCKED_LOGIN,
    ApplicationRunStatuses.BLOCKED_CAPTCHA: ApplicationStatuses.AGENT_BLOCKED_CAPTCHA,
    ApplicationRunStatuses.UNSUPPORTED_PORTAL: ApplicationStatuses.AGENT_UNSUPPORTED_PORTAL,
    ApplicationRunStatuses.SKIPPED_DUPLICATE: ApplicationStatuses.AGENT_SKIPPED_DUPLICATE,
    ApplicationRunStatuses.FAILED: ApplicationStatuses.AGENT_FAILED,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def extract_apply_url(job: dict[str, Any] | None, saved_row: dict[str, Any] | None = None) -> str | None:
    for source in (saved_row or {}, job or {}):
        for key in ("apply_url", "job_url", "url", "source_url"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return None


def portal_domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        return (parsed.netloc or parsed.path.split("/")[0]).lower().replace("www.", "") or None
    except Exception:
        return None


class ApplicationAgentService:
    """Application-agent orchestration service.

    The default strategy is intentionally conservative. It does not falsely mark
    generic external applications as submitted. Add portal-specific strategies
    for Lever/Greenhouse/Workday/etc. and register them in select_strategy_node.
    """

    def __init__(self, *, request_id: str | None = None):
        self.request_id = request_id or new_uuid()
        self.external_submit_enabled = os.getenv("JOB_MINER_APPLICATION_AGENT_ENABLE_EXTERNAL_SUBMIT", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.auto_submit_domains = {
            item.strip().lower().replace("www.", "")
            for item in os.getenv("JOB_MINER_APPLICATION_AGENT_AUTOSUBMIT_DOMAINS", "careers.strategicstaff.com").split(",")
            if item.strip()
        }

    def create_batch_for_saved_jobs(
        self,
        *,
        app_user_id: str,
        candidate_id: str,
        job_ids: list[str] | None = None,
        max_jobs: int = 25,
        require_review_before_submit: bool = False,
    ) -> dict[str, Any]:
        with postgres_session() as session:
            repo = ControlRepository(session)
            active = repo.get_active_application_batch_for_candidate(app_user_id, candidate_id)
            if active:
                return {
                    "batch": active,
                    "job_runs": repo.list_application_job_runs(str(active["id"])),
                    "already_running": True,
                }

            eligible = repo.list_agent_eligible_saved_jobs(
                app_user_id,
                candidate_id,
                job_ids=job_ids,
                max_jobs=max_jobs,
            )
            batch = repo.create_application_batch(
                app_user_id=app_user_id,
                candidate_id=candidate_id,
                requested_job_count=len(eligible),
                metadata={
                    "request_id": self.request_id,
                    "require_review_before_submit": require_review_before_submit,
                    "requested_job_ids": job_ids or [],
                    "external_submit_enabled": self.external_submit_enabled,
                },
            )

            job_runs: list[dict[str, Any]] = []
            for saved in eligible:
                job_id = str(saved["job_id"])
                job = get_job_by_id(job_id)
                apply_url = extract_apply_url(job, saved)
                existing_status = str(saved.get("existing_application_status") or "").strip().lower()
                existing_application_id = saved.get("existing_application_id")

                if existing_status in TERMINAL_APPLICATION_STATUSES and existing_application_id:
                    run = repo.create_application_job_run(
                        batch_id=str(batch["id"]),
                        app_user_id=app_user_id,
                        candidate_id=candidate_id,
                        job_id=job_id,
                        application_id=str(existing_application_id),
                        apply_url=apply_url or saved.get("existing_application_apply_url"),
                        portal_domain=portal_domain_from_url(apply_url or saved.get("existing_application_apply_url")),
                        apply_strategy="already_applied",
                        metadata={
                            "request_id": self.request_id,
                            "saved_job_id": str(saved.get("id")),
                            "existing_application_status": existing_status,
                        },
                    )
                    run = repo.update_application_job_run_status(
                        str(run["id"]),
                        ApplicationRunStatuses.SKIPPED_DUPLICATE,
                        apply_strategy="already_applied",
                        portal_domain=portal_domain_from_url(apply_url or saved.get("existing_application_apply_url")),
                        error_type="already_applied",
                        error_message="This saved job already has a completed application record, so the agent skipped it.",
                        metadata={"request_id": self.request_id, "existing_application_status": existing_status},
                    ) or run
                    repo.add_application_event(
                        app_user_id=app_user_id,
                        candidate_id=candidate_id,
                        job_id=job_id,
                        event_type="agent_application_skipped_duplicate",
                        application_id=str(existing_application_id),
                        message="Application agent skipped this saved job because it was already applied.",
                        payload={
                            "batch_id": str(batch["id"]),
                            "job_run_id": str(run["id"]),
                            "request_id": self.request_id,
                            "existing_application_status": existing_status,
                        },
                    )
                    job_runs.append(run)
                    continue

                app_row = repo.create_or_update_application(
                    app_user_id,
                    candidate_id,
                    job_id,
                    match_run_id=saved.get("match_run_id"),
                    apply_url=apply_url,
                    status=ApplicationStatuses.AGENT_APPLY_REQUESTED,
                )
                repo.add_application_event(
                    app_user_id=app_user_id,
                    candidate_id=candidate_id,
                    job_id=job_id,
                    event_type="agent_apply_requested",
                    application_id=str(app_row["id"]),
                    message="Application agent queued this saved job.",
                    payload={
                        "batch_id": str(batch["id"]),
                        "request_id": self.request_id,
                        "previous_application_status": existing_status or None,
                    },
                )
                job_runs.append(
                    repo.create_application_job_run(
                        batch_id=str(batch["id"]),
                        app_user_id=app_user_id,
                        candidate_id=candidate_id,
                        job_id=job_id,
                        application_id=str(app_row["id"]),
                        apply_url=apply_url,
                        portal_domain=portal_domain_from_url(apply_url),
                        apply_strategy=None,
                        metadata={
                            "request_id": self.request_id,
                            "saved_job_id": str(saved.get("id")),
                            "previous_application_status": existing_status or None,
                            "require_review_before_submit": require_review_before_submit,
                        },
                    )
                )

            repo.refresh_application_batch_counts(str(batch["id"]))
            batch = repo.get_application_batch(str(batch["id"])) or batch
            return {"batch": batch, "job_runs": job_runs, "already_running": False}

    def nodes(self) -> dict[str, Any]:
        return {
            "load_context": self.load_context_node,
            "preflight_validate": self.preflight_validate_node,
            "select_strategy": self.select_strategy_node,
            "prepare_application": self.prepare_application_node,
            "submit_application": self.submit_application_node,
            "persist_result": self.persist_result_node,
        }

    def run_job_application(self, *, job_run_id: str) -> JobApplicationState:
        with postgres_session() as session:
            repo = ControlRepository(session)
            run = repo.get_application_job_run(job_run_id)
            if not run:
                raise ValueError(f"Application job run not found: {job_run_id}")
            initial: JobApplicationState = {
                "request_id": self.request_id,
                "batch_id": str(run["batch_id"]),
                "job_run_id": str(run["id"]),
                "application_id": str(run["application_id"]) if run.get("application_id") else None,
                "app_user_id": str(run["app_user_id"]),
                "candidate_id": str(run["candidate_id"]),
                "job_id": str(run["job_id"]),
                "apply_url": run.get("apply_url"),
                "portal_domain": run.get("portal_domain"),
                "langgraph_thread_id": run.get("langgraph_thread_id") or f"application-job-{run['id']}",
                "metadata": {"request_id": self.request_id, **(run.get("metadata") or {})},
            }
            repo.set_application_job_run_task(str(run["id"]), str(run.get("celery_task_uuid") or ""), langgraph_thread_id=initial["langgraph_thread_id"])

        return invoke_job_application_graph(initial_state=initial, nodes=self.nodes())

    def load_context_node(self, state: JobApplicationState) -> JobApplicationState:
        job_id = str(state["job_id"])
        candidate_id = str(state["candidate_id"])
        job = get_job_by_id(job_id)
        candidate = get_candidate_profile(candidate_id)
        candidate_tower = (candidate or {}).get("candidate_tower") or None
        resume = (candidate or {}).get("resume_profile") or None
        apply_url = extract_apply_url(job, {"apply_url": state.get("apply_url")})
        portal_domain = portal_domain_from_url(apply_url)
        external_account = None
        with postgres_session() as session:
            repo = ControlRepository(session)
            if portal_domain:
                external_account = repo.get_candidate_external_account(str(state["app_user_id"]), candidate_id, portal_domain)
            repo.update_application_job_run_status(
                str(state["job_run_id"]),
                ApplicationRunStatuses.RUNNING,
                portal_domain=portal_domain,
                metadata={"request_id": self.request_id, "node": "load_context"},
            )
            if state.get("application_id"):
                repo.update_application_status_by_id(
                    str(state["application_id"]),
                    ApplicationStatuses.AGENT_RUNNING,
                    metadata={"request_id": self.request_id, "batch_id": state.get("batch_id")},
                )
        return {
            **state,
            "job": job,
            "candidate_profile": candidate_tower,
            "resume_profile": resume,
            "apply_url": apply_url,
            "portal_domain": portal_domain,
            "external_account": external_account,
            "run_status": ApplicationRunStatuses.RUNNING,
            "application_status": ApplicationStatuses.AGENT_RUNNING,
        }

    def preflight_validate_node(self, state: JobApplicationState) -> JobApplicationState:
        if state.get("terminal"):
            return state
        if not state.get("job"):
            return self._terminal(
                state,
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="Job record was not found in Mongo jobs_current.",
                error_type="job_not_found",
            )
        if not state.get("candidate_profile"):
            return self._terminal(
                state,
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="Candidate profile was not found or is not ready.",
                error_type="candidate_profile_missing",
            )
        if not state.get("apply_url"):
            return self._terminal(
                state,
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="No apply URL is available for this job.",
                error_type="missing_apply_url",
            )
        return {**state, "message": "Preflight validation passed."}

    def select_strategy_node(self, state: JobApplicationState) -> JobApplicationState:
        if state.get("terminal"):
            return state
        domain = str(state.get("portal_domain") or "").lower().replace("www.", "")
        strategy = self._select_strategy(domain)
        strategy_job = {**(state.get("job") or {}), "__resume_profile": state.get("resume_profile") or {}}
        decision = strategy.can_apply(
            job=strategy_job,
            candidate_profile=state.get("candidate_profile") or {},
            apply_url=state.get("apply_url"),
        )
        next_state: JobApplicationState = {
            **state,
            "strategy_key": decision.strategy_key,
            "metadata": {**(state.get("metadata") or {}), **decision.metadata},
        }
        if (state.get("metadata") or {}).get("require_review_before_submit"):
            return self._terminal(
                next_state,
                run_status=ApplicationRunStatuses.NEEDS_REVIEW,
                application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
                message="Batch was configured for review-before-submit, so the agent prepared this job for manual review instead of submitting.",
                error_type="review_before_submit_enabled",
                metadata=decision.metadata,
            )
        if not decision.can_submit:
            return self._terminal(
                next_state,
                run_status=decision.run_status or ApplicationRunStatuses.NEEDS_REVIEW,
                application_status=decision.application_status or ApplicationStatuses.AGENT_NEEDS_REVIEW,
                message=decision.message or "This job needs candidate review before application.",
                error_type=decision.error_type or "manual_review_required",
                metadata=decision.metadata,
            )
        if not self.external_submit_enabled or domain not in self.auto_submit_domains:
            return self._terminal(
                next_state,
                run_status=ApplicationRunStatuses.NEEDS_REVIEW,
                application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
                message=(
                    f"Auto-submit is available for {domain}, but it is disabled. "
                    "Set JOB_MINER_APPLICATION_AGENT_ENABLE_EXTERNAL_SUBMIT=true and include the domain in "
                    "JOB_MINER_APPLICATION_AGENT_AUTOSUBMIT_DOMAINS for the demo."
                ),
                error_type="external_submit_disabled",
                metadata={"portal_domain": domain, **decision.metadata},
            )
        return next_state

    def prepare_application_node(self, state: JobApplicationState) -> JobApplicationState:
        if state.get("terminal"):
            return state
        return {
            **state,
            "metadata": {
                **(state.get("metadata") or {}),
                "prepared_on": utc_now().isoformat(),
                "resume_id": (state.get("resume_profile") or {}).get("resume_id"),
            },
        }

    def submit_application_node(self, state: JobApplicationState) -> JobApplicationState:
        if state.get("terminal"):
            return state
        strategy = self._select_strategy(str(state.get("portal_domain") or "").lower().replace("www.", ""))
        job_payload = {
            **(state.get("job") or {}),
            "__candidate_id": state.get("candidate_id"),
            "__resume_profile": state.get("resume_profile") or {},
            "__external_account": state.get("external_account") or {},
            "__job_run_id": state.get("job_run_id"),
        }
        result = strategy.submit(
            job=job_payload,
            candidate_profile=state.get("candidate_profile") or {},
            apply_url=state.get("apply_url"),
        )
        return self._terminal(
            state,
            run_status=result.run_status,
            application_status=result.application_status,
            message=result.message,
            error_type=result.error_type,
            error_message=result.error_message,
            external_confirmation_id=result.external_confirmation_id,
            metadata=result.metadata,
        )

    def persist_result_node(self, state: JobApplicationState) -> JobApplicationState:
        run_status = state.get("run_status") or ApplicationRunStatuses.FAILED
        application_status = state.get("application_status") or RUN_STATUS_TO_APPLICATION_STATUS.get(run_status) or ApplicationStatuses.AGENT_FAILED
        with postgres_session() as session:
            repo = ControlRepository(session)
            run = repo.update_application_job_run_status(
                str(state["job_run_id"]),
                run_status,
                apply_strategy=state.get("strategy_key"),
                portal_domain=state.get("portal_domain"),
                error_type=state.get("error_type"),
                error_message=state.get("error_message") or state.get("message"),
                external_confirmation_id=state.get("external_confirmation_id"),
                metadata={"request_id": self.request_id, **(state.get("metadata") or {})},
            )
            if state.get("application_id"):
                repo.update_application_status_by_id(
                    str(state["application_id"]),
                    application_status,
                    applied=run_status == ApplicationRunStatuses.SUBMITTED,
                    metadata={
                        "request_id": self.request_id,
                        "batch_id": state.get("batch_id"),
                        "job_run_id": state.get("job_run_id"),
                        "message": state.get("message"),
                        "portal_domain": state.get("portal_domain"),
                        "strategy_key": state.get("strategy_key"),
                    },
                )
                if run_status == ApplicationRunStatuses.SUBMITTED:
                    moved_saved = repo.mark_saved_job_applied(
                        str(state["app_user_id"]),
                        str(state["candidate_id"]),
                        str(state["job_id"]),
                        application_id=str(state["application_id"]),
                        job_run_id=str(state["job_run_id"]),
                    )
                    if moved_saved:
                        repo.add_application_event(
                            app_user_id=str(state["app_user_id"]),
                            candidate_id=str(state["candidate_id"]),
                            job_id=str(state["job_id"]),
                            application_id=str(state["application_id"]),
                            event_type="saved_job_moved_to_applications",
                            message="Application agent submitted this job successfully, so it was moved from Saved Jobs to Applications.",
                            payload={
                                "batch_id": state.get("batch_id"),
                                "job_run_id": state.get("job_run_id"),
                                "saved_job_id": str(moved_saved.get("id")),
                                "run_status": run_status,
                                "application_status": application_status,
                            },
                        )
                repo.add_application_event(
                    app_user_id=str(state["app_user_id"]),
                    candidate_id=str(state["candidate_id"]),
                    job_id=str(state["job_id"]),
                    application_id=str(state["application_id"]),
                    event_type=f"agent_application_{run_status}",
                    message=state.get("message"),
                    payload={
                        "batch_id": state.get("batch_id"),
                        "job_run_id": state.get("job_run_id"),
                        "run_status": run_status,
                        "application_status": application_status,
                        "error_type": state.get("error_type"),
                        "strategy_key": state.get("strategy_key"),
                    },
                )
            repo.refresh_application_batch_counts(str(state["batch_id"]))
        return {**state, "run_status": run_status, "application_status": application_status, "metadata": {**(state.get("metadata") or {}), "persisted": bool(run)}}

    def finalize_batch(self, *, batch_id: str) -> dict[str, Any]:
        with postgres_session() as session:
            repo = ControlRepository(session)
            batch = repo.refresh_application_batch_counts(batch_id)
            if not batch:
                raise ValueError(f"Application batch not found: {batch_id}")
            job_runs = repo.list_application_job_runs(batch_id)
            notification = ApplicationNotificationService(repo).create_batch_summary(batch=batch, job_runs=job_runs)
            repo.add_application_event(
                app_user_id=str(batch["app_user_id"]),
                candidate_id=str(batch["candidate_id"]),
                job_id="__batch__",
                event_type="agent_batch_summary_created",
                message="Application agent batch summary is visible in the candidate portal. Email sending is currently a placeholder.",
                payload={"batch_id": batch_id, "notification_id": str(notification["id"])},
            )
            return {"batch": batch, "job_runs": job_runs, "notification": notification}


    def _select_strategy(self, domain: str):
        normalized = str(domain or "").lower().replace("www.", "")
        if normalized in StrategicStaffDirectApplyStrategy.supported_domains:
            return StrategicStaffDirectApplyStrategy()
        return GenericExternalReviewStrategy() if normalized else UnsupportedPortalStrategy()

    def _terminal(
        self,
        state: JobApplicationState,
        *,
        run_status: str,
        application_status: str,
        message: str,
        error_type: str | None = None,
        error_message: str | None = None,
        external_confirmation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> JobApplicationState:
        return {
            **state,
            "terminal": True,
            "run_status": run_status,
            "application_status": application_status,
            "message": message,
            "error_type": error_type,
            "error_message": error_message or message,
            "external_confirmation_id": external_confirmation_id,
            "metadata": {**(state.get("metadata") or {}), **(metadata or {})},
        }
