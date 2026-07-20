from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from ..crawl.browser_evidence import (
    BrowserEvidenceCollector,
    BrowserEvidenceOptions,
    BrowserEvidenceReport,
)
from ..schemas import BrowserSettings
from .contracts import CompletenessState, DiscoveryBatch, ScrapeStrategy
from .dom_discovery import DomCandidateDiscoverer, DomDiscoveryOptions


_ACCESS_BLOCK_STATUS_CODES = {401, 403, 407, 429}


class AdaptiveDomDiscoveryService:
    """Capture one rendered listing and infer jobs from repeated DOM structure.

    This is a bounded fallback lane. It does not persist XPath/CSS selectors,
    reuse authenticated state, solve CAPTCHAs, or claim pagination completeness.
    """

    def __init__(
        self,
        browser_settings: BrowserSettings,
        *,
        evidence_options: BrowserEvidenceOptions | None = None,
        discovery_options: DomDiscoveryOptions | None = None,
        collector: Any | None = None,
    ) -> None:
        self.browser_settings = browser_settings
        self.evidence_options = evidence_options or BrowserEvidenceOptions()
        self.discovery_options = discovery_options or DomDiscoveryOptions()
        self.collector = collector

    async def discover(
        self,
        listing_url: str,
        *,
        allowed_hosts: Iterable[str],
        max_candidates: int,
    ) -> DiscoveryBatch:
        collector = self.collector or BrowserEvidenceCollector(
            self.browser_settings,
            options=self.evidence_options,
        )
        report = await collector.capture(listing_url, allowed_hosts=tuple(allowed_hosts))
        terminal = self._terminal_batch(report)
        if terminal is not None:
            return terminal

        options = replace(self.discovery_options, max_candidates=max_candidates)
        batch = DomCandidateDiscoverer(options).discover(
            report,
            listing_url=report.final_url,
        )
        metrics = dict(batch.metrics)
        metrics["browser_evidence"] = self._report_metrics(report)
        reasons = list(batch.reasons)
        reasons.extend(str(error)[:500] for error in report.errors[:5])
        return batch.model_copy(update={"metrics": metrics, "reasons": reasons})

    @classmethod
    def _terminal_batch(cls, report: BrowserEvidenceReport) -> DiscoveryBatch | None:
        metrics = {"browser_evidence": cls._report_metrics(report)}
        if report.status_code in _ACCESS_BLOCK_STATUS_CODES:
            return DiscoveryBatch(
                strategy=ScrapeStrategy.BLUEPRINT_DOM,
                completeness=CompletenessState.BLOCKED,
                pages_visited=1,
                reasons=[
                    f"Public browser access was blocked with HTTP {report.status_code}; "
                    "no anti-bot bypass was attempted."
                ],
                metrics=metrics,
            )
        if not report.success and report.node_count == 0:
            reasons = ["Browser evidence capture produced no usable DOM nodes."]
            reasons.extend(str(error)[:500] for error in report.errors[:5])
            return DiscoveryBatch(
                strategy=ScrapeStrategy.BLUEPRINT_DOM,
                completeness=CompletenessState.FAILED,
                pages_visited=1,
                reasons=reasons,
                metrics=metrics,
            )
        return None

    @staticmethod
    def _report_metrics(report: BrowserEvidenceReport) -> dict[str, Any]:
        return {
            "success": report.success,
            "status_code": report.status_code,
            "final_url": report.final_url,
            "frames": len(report.frames),
            "nodes": report.node_count,
            "linkless_clickables": report.linkless_clickable_count,
            "network_json": len(report.network_json),
            "truncated_frames": int(report.metrics.get("truncated_frames") or 0),
            "errors": len(report.errors),
        }
