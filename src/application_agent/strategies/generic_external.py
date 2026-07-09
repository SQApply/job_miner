from __future__ import annotations

from .base import StrategyDecision, SubmissionResult
from ...common.constants import ApplicationRunStatuses, ApplicationStatuses


class GenericExternalReviewStrategy:
    """Production-safe default strategy.

    A generic job URL cannot be safely auto-submitted without a portal-specific
    contract/form strategy. This strategy therefore creates a candidate-visible
    review item instead of pretending that the application was submitted.
    """

    strategy_key = "generic_external_manual_review"

    def can_apply(self, *, job: dict, candidate_profile: dict, apply_url: str | None) -> StrategyDecision:
        if not apply_url:
            return StrategyDecision(
                strategy_key=self.strategy_key,
                can_submit=False,
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="This saved job does not have an apply URL.",
                error_type="missing_apply_url",
            )
        return StrategyDecision(
            strategy_key=self.strategy_key,
            can_submit=False,
            run_status=ApplicationRunStatuses.NEEDS_REVIEW,
            application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
            message=(
                "Manual review is required before submitting this external job. "
                "Add a portal-specific strategy to enable safe auto-submit."
            ),
            error_type="portal_strategy_not_configured",
            metadata={"apply_url": apply_url},
        )

    def submit(self, *, job: dict, candidate_profile: dict, apply_url: str | None) -> SubmissionResult:
        decision = self.can_apply(job=job, candidate_profile=candidate_profile, apply_url=apply_url)
        return SubmissionResult(
            run_status=decision.run_status or ApplicationRunStatuses.NEEDS_REVIEW,
            application_status=decision.application_status or ApplicationStatuses.AGENT_NEEDS_REVIEW,
            message=decision.message or "Manual review required.",
            error_type=decision.error_type,
            error_message=decision.message,
            metadata=decision.metadata,
        )
