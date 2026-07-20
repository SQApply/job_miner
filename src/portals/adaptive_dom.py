from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from ..crawl.browser_evidence import (
    BrowserEvidenceCollector,
    BrowserEvidenceOptions,
    BrowserEvidenceReport,
    BrowserEvidenceSession,
)
from ..schemas import BrowserSettings
from .contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    ScrapeStrategy,
)
from .dom_discovery import DomCandidateDiscoverer, DomDiscoveryOptions
from .json_discovery import JsonCandidateDiscoverer, JsonDiscoveryOptions
from .live_interaction import LinklessInteractionOptions, LiveLinklessResolver


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
        structured_options: JsonDiscoveryOptions | None = None,
        interaction_options: LinklessInteractionOptions | None = None,
        collector: Any | None = None,
        interaction_resolver: Any | None = None,
    ) -> None:
        self.browser_settings = browser_settings
        self.evidence_options = evidence_options or BrowserEvidenceOptions()
        self.discovery_options = discovery_options or DomDiscoveryOptions()
        self.structured_options = structured_options or JsonDiscoveryOptions()
        self.interaction_options = interaction_options or LinklessInteractionOptions()
        self.collector = collector
        self.interaction_resolver = interaction_resolver

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
        approved_hosts = tuple(allowed_hosts)
        capture_session = getattr(collector, "capture_session", None)
        if callable(capture_session):
            async with capture_session(
                listing_url,
                allowed_hosts=approved_hosts,
            ) as session:
                return await self._discover_report(
                    session.report,
                    listing_url=listing_url,
                    max_candidates=max_candidates,
                    session=session,
                )

        report = await collector.capture(listing_url, allowed_hosts=approved_hosts)
        return await self._discover_report(
            report,
            listing_url=listing_url,
            max_candidates=max_candidates,
            session=None,
        )

    async def _discover_report(
        self,
        report: BrowserEvidenceReport,
        *,
        listing_url: str,
        max_candidates: int,
        session: BrowserEvidenceSession | None,
    ) -> DiscoveryBatch:
        terminal = self._terminal_batch(report)
        if terminal is not None:
            return terminal

        dom_options = replace(self.discovery_options, max_candidates=max_candidates)
        dom_discoverer = DomCandidateDiscoverer(dom_options)
        dom_batch = dom_discoverer.discover(
            report,
            listing_url=report.final_url,
        )
        structured_options = replace(
            self.structured_options,
            max_candidates=max_candidates,
        )
        structured_batch = JsonCandidateDiscoverer(structured_options).discover(report)
        batch = self._merge_evidence_batches(
            dom_batch,
            structured_batch,
            max_candidates=max_candidates,
        )
        metrics = dict(batch.metrics)
        metrics["browser_evidence"] = self._report_metrics(report)
        reasons = list(batch.reasons)
        reasons.extend(str(error)[:500] for error in report.errors[:5])

        interaction_metrics: dict[str, Any] = {
            "available": session is not None,
            "attempted": 0,
            "resolved": 0,
        }
        if session is not None and batch.linkless_candidates:
            resolver = self.interaction_resolver or LiveLinklessResolver(
                options=self.interaction_options,
                discoverer=dom_discoverer,
            )
            try:
                interaction_batch = await resolver.resolve(
                    session,
                    batch.linkless_candidates,
                    listing_url=report.final_url,
                )
            except Exception as exc:
                interaction_metrics.update(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:500],
                    }
                )
                reasons.append(
                    f"Bounded linkless interaction failed: {type(exc).__name__}: {exc}"[:1_000]
                )
            else:
                interaction_metrics = dict(interaction_batch.metrics)
                interaction_metrics["available"] = True
                resolved_source_ids = set(
                    str(value)
                    for value in interaction_metrics.get(
                        "resolved_source_candidate_ids",
                        [],
                    )
                )
                retained = [
                    candidate
                    for candidate in batch.candidates
                    if candidate.candidate_id not in resolved_source_ids
                ]
                batch = self._batch_from_candidates(
                    [*interaction_batch.candidates, *retained],
                    base=batch,
                    max_candidates=max_candidates,
                )
                reasons.extend(interaction_batch.reasons)

        metrics = dict(batch.metrics)
        metrics["browser_evidence"] = self._report_metrics(report)
        metrics["linkless_interaction"] = interaction_metrics
        metrics["candidates"] = len(batch.candidates)
        metrics["url_candidates"] = len(batch.discovered_urls)
        metrics["linkless_candidates"] = len(batch.linkless_candidates)
        return batch.model_copy(
            update={
                "completeness": CompletenessState.PARTIAL,
                "pagination_complete": False,
                "metrics": metrics,
                "reasons": list(dict.fromkeys(reasons)),
            }
        )

    @classmethod
    def _merge_evidence_batches(
        cls,
        dom_batch: DiscoveryBatch,
        structured_batch: DiscoveryBatch,
        *,
        max_candidates: int,
    ) -> DiscoveryBatch:
        merged = cls._select_candidates(
            [*structured_batch.candidates, *dom_batch.candidates],
            max_candidates=max_candidates,
        )
        metrics = dict(dom_batch.metrics)
        metrics.update(
            {
                "dom_candidates": len(dom_batch.candidates),
                "structured_candidates": len(structured_batch.candidates),
                "structured_discovery": dict(structured_batch.metrics),
                "merged_candidates": len(merged),
            }
        )
        strategy = (
            structured_batch.strategy
            if structured_batch.candidates and not dom_batch.candidates
            else ScrapeStrategy.BLUEPRINT_DOM
        )
        return DiscoveryBatch(
            strategy=strategy,
            completeness=CompletenessState.PARTIAL,
            candidates=merged,
            pages_visited=1,
            pagination_complete=False,
            reasons=list(dict.fromkeys([*dom_batch.reasons, *structured_batch.reasons])),
            metrics=metrics,
        )

    @classmethod
    def _batch_from_candidates(
        cls,
        candidates: list[DiscoveryCandidate],
        *,
        base: DiscoveryBatch,
        max_candidates: int,
    ) -> DiscoveryBatch:
        selected = cls._select_candidates(candidates, max_candidates=max_candidates)
        metrics = dict(base.metrics)
        metrics["post_interaction_candidates"] = len(selected)
        return base.model_copy(update={"candidates": selected, "metrics": metrics})

    @staticmethod
    def _select_candidates(
        candidates: list[DiscoveryCandidate],
        *,
        max_candidates: int,
    ) -> list[DiscoveryCandidate]:
        selected: dict[str, DiscoveryCandidate] = {}
        order: dict[str, int] = {}
        for position, candidate in enumerate(candidates):
            key = candidate.detail_url or candidate.identity_key
            existing = selected.get(key)
            should_replace = existing is None or (
                candidate.preextracted_job is not None
                and existing.preextracted_job is None
            ) or candidate.confidence > existing.confidence
            if should_replace:
                selected[key] = candidate
                order.setdefault(key, position)
        ranked = sorted(
            selected.values(),
            key=lambda candidate: (
                -int(candidate.preextracted_job is not None),
                -int(bool(candidate.evidence.get("evidence_preserving"))),
                -candidate.confidence,
                order[candidate.detail_url or candidate.identity_key],
            ),
        )
        return ranked[:max_candidates]

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
        structured_documents = sum(
            evidence.payload is not None and not evidence.truncated
            for evidence in report.network_json
        ) + sum(
            evidence.payload is not None and not evidence.truncated
            for frame in report.frames
            for evidence in frame.inline_json
        )
        if not report.success and report.node_count == 0 and not structured_documents:
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
            "inline_json": sum(len(frame.inline_json) for frame in report.frames),
            "truncated_frames": int(report.metrics.get("truncated_frames") or 0),
            "errors": len(report.errors),
        }
