from __future__ import annotations

from .base import StrategyDecision, SubmissionResult
from ...common.constants import ApplicationRunStatuses, ApplicationStatuses


class UnsupportedPortalStrategy:
    strategy_key = "unsupported_portal"

    def can_apply(self, *, job: dict, candidate_profile: dict, apply_url: str | None) -> StrategyDecision:
        return StrategyDecision(
            strategy_key=self.strategy_key,
            can_submit=False,
            run_status=ApplicationRunStatuses.UNSUPPORTED_PORTAL,
            application_status=ApplicationStatuses.AGENT_UNSUPPORTED_PORTAL,
            message="This portal is not supported for automated application yet.",
            error_type="unsupported_portal",
        )

    def submit(self, *, job: dict, candidate_profile: dict, apply_url: str | None) -> SubmissionResult:
        decision = self.can_apply(job=job, candidate_profile=candidate_profile, apply_url=apply_url)
        return SubmissionResult(
            run_status=decision.run_status or ApplicationRunStatuses.UNSUPPORTED_PORTAL,
            application_status=decision.application_status or ApplicationStatuses.AGENT_UNSUPPORTED_PORTAL,
            message=decision.message or "Unsupported portal.",
            error_type=decision.error_type,
            error_message=decision.message,
        )
