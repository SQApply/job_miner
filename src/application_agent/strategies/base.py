from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class StrategyDecision:
    strategy_key: str
    can_submit: bool
    run_status: str | None = None
    application_status: str | None = None
    message: str | None = None
    error_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmissionResult:
    run_status: str
    application_status: str
    message: str
    external_confirmation_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ApplicationStrategy(Protocol):
    strategy_key: str

    def can_apply(self, *, job: dict[str, Any], candidate_profile: dict[str, Any], apply_url: str | None) -> StrategyDecision:
        ...

    def submit(self, *, job: dict[str, Any], candidate_profile: dict[str, Any], apply_url: str | None) -> SubmissionResult:
        ...
