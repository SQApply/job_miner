from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

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
from .dom_pagination import (
    GeneralizedDomPaginationDriver,
    GeneralizedDomPaginationOptions,
)
from .json_discovery import JsonCandidateDiscoverer, JsonDiscoveryOptions
from .live_interaction import LinklessInteractionOptions, LiveLinklessResolver
from .url_intelligence import assess_job_candidate_url


_ACCESS_BLOCK_STATUS_CODES = {401, 403, 407, 429}
_LISTING_EXPANSION_ROUTE = re.compile(
    r"/(?:jobs?|careers?|openings?|opportunities|employment|search)(?:/|$)",
    re.I,
)
_LISTING_EXPANSION_REJECT = re.compile(
    r"\b(job\s*cart|alerts?|submit\s+(?:your\s+)?resume|upload\s+resume|"
    r"benefits?|employers?|salary\s+guide|resources?)\b",
    re.I,
)


class AdaptiveDomDiscoveryService:
    """Infer jobs from rendered DOM structure and bounded public interaction.

    It does not persist XPath/CSS selectors, reuse authenticated state, or solve
    CAPTCHAs. Complete-catalog callers may request generalized pagination; that
    lane claims completeness only after observable UI exhaustion.
    """

    def __init__(
        self,
        browser_settings: BrowserSettings,
        *,
        evidence_options: BrowserEvidenceOptions | None = None,
        discovery_options: DomDiscoveryOptions | None = None,
        structured_options: JsonDiscoveryOptions | None = None,
        interaction_options: LinklessInteractionOptions | None = None,
        pagination_options: GeneralizedDomPaginationOptions | None = None,
        collector: Any | None = None,
        interaction_resolver: Any | None = None,
        pagination_driver: Any | None = None,
    ) -> None:
        self.browser_settings = browser_settings
        self.evidence_options = evidence_options or BrowserEvidenceOptions()
        self.discovery_options = discovery_options or DomDiscoveryOptions()
        self.structured_options = structured_options or JsonDiscoveryOptions()
        self.interaction_options = interaction_options or LinklessInteractionOptions()
        self.pagination_options = pagination_options or GeneralizedDomPaginationOptions()
        self.collector = collector
        self.interaction_resolver = interaction_resolver
        self.pagination_driver = pagination_driver

    async def discover(
        self,
        listing_url: str,
        *,
        allowed_hosts: Iterable[str],
        max_candidates: int,
        max_pages: int = 50,
        require_complete: bool = False,
    ) -> DiscoveryBatch:
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
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
                if require_complete:
                    # Preserve the listing context while pagination is being
                    # exhausted. Linkless card clicks are intentionally not run
                    # between pages because they can navigate away from it.
                    batch = await self._discover_report(
                        session.report,
                        listing_url=listing_url,
                        max_candidates=max_candidates,
                        session=None,
                    )
                    batch = await self._exhaust_live_listing(
                        session,
                        batch,
                        listing_url=listing_url,
                        max_candidates=max_candidates,
                        max_pages=max_pages,
                    )
                else:
                    batch = await self._discover_report(
                        session.report,
                        listing_url=listing_url,
                        max_candidates=max_candidates,
                        session=session,
                    )
            return await self._expand_listing_routes(
                collector,
                batch,
                listing_url=listing_url,
                allowed_hosts=approved_hosts,
                max_candidates=max_candidates,
            )

        report = await collector.capture(listing_url, allowed_hosts=approved_hosts)
        batch = await self._discover_report(
            report,
            listing_url=listing_url,
            max_candidates=max_candidates,
            session=None,
        )
        if require_complete:
            reasons = list(batch.reasons)
            reasons.append(
                "Complete discovery requested, but the evidence collector did not expose "
                "a live public browser session for pagination exhaustion."
            )
            batch = batch.model_copy(
                update={
                    "completeness": CompletenessState.PARTIAL,
                    "pagination_complete": False,
                    "reasons": list(dict.fromkeys(reasons)),
                }
            )
        return await self._expand_listing_routes(
            collector,
            batch,
            listing_url=listing_url,
            allowed_hosts=approved_hosts,
            max_candidates=max_candidates,
        )

    async def _exhaust_live_listing(
        self,
        session: BrowserEvidenceSession,
        batch: DiscoveryBatch,
        *,
        listing_url: str,
        max_candidates: int,
        max_pages: int,
    ) -> DiscoveryBatch:
        if batch.completeness in {CompletenessState.BLOCKED, CompletenessState.FAILED}:
            return batch

        driver = self.pagination_driver or GeneralizedDomPaginationDriver(
            self.pagination_options
        )
        selected = self._select_candidates(
            list(batch.candidates),
            max_candidates=max_candidates,
        )
        reasons = list(batch.reasons)
        errors: list[str] = []
        pages_captured = 1
        actions: list[dict[str, Any]] = []
        growth_rounds = 0
        stable_rounds = 0
        stalled_control_rounds = 0
        previous_height = 0
        terminal_reason: str | None = None
        pagination_complete = False
        candidate_cap_reached = len(selected) >= max_candidates

        for round_number in range(1, max_pages):
            try:
                step = await driver.advance(session, requested_url=listing_url)
            except Exception as exc:
                errors.append(
                    f"generalized_pagination_failed: {type(exc).__name__}: {exc}"[:1_000]
                )
                terminal_reason = "pagination_driver_failed"
                break

            pages_captured += 1
            errors.extend(step.errors)
            page_batch = await self._discover_report(
                step.report,
                listing_url=listing_url,
                max_candidates=max_candidates,
                session=None,
            )
            if page_batch.completeness in {
                CompletenessState.BLOCKED,
                CompletenessState.FAILED,
            }:
                reasons.extend(page_batch.reasons)
                terminal_reason = "post_interaction_page_unavailable"
                break

            before_count = len(selected)
            selected = self._select_candidates(
                [*selected, *page_batch.candidates],
                max_candidates=max_candidates,
            )
            reasons.extend(page_batch.reasons)
            growth = len(selected) > before_count
            if growth:
                growth_rounds += 1

            state = dict(step.state)
            height = int(state.get("height") or 0)
            enabled_controls = int(state.get("enabled_controls") or 0)
            disabled_controls = int(state.get("disabled_controls") or 0)
            at_bottom = state.get("at_bottom") is True
            actions.append(
                {
                    "round": round_number,
                    "action": step.action,
                    "label": step.label,
                    "candidate_growth": len(selected) - before_count,
                    "candidate_count": len(selected),
                    "height": height,
                    "at_bottom": at_bottom,
                    "enabled_controls": enabled_controls,
                    "disabled_controls": disabled_controls,
                }
            )

            candidate_cap_reached = len(selected) >= max_candidates
            if candidate_cap_reached:
                terminal_reason = "candidate_safety_cap_reached"
                break

            stable_surface = (
                not growth
                and at_bottom
                and enabled_controls == 0
                and (previous_height == 0 or height <= previous_height)
            )
            stable_rounds = stable_rounds + 1 if stable_surface else 0
            stalled_control_rounds = (
                stalled_control_rounds + 1
                if not growth and enabled_controls > 0
                else 0
            )

            if disabled_controls > 0 and enabled_controls == 0:
                pagination_complete = True
                terminal_reason = "terminal_pagination_control_disabled"
                break
            if stable_rounds >= self.pagination_options.stable_rounds_required:
                pagination_complete = True
                terminal_reason = "stable_bottom_without_pagination_control"
                break
            if stalled_control_rounds >= self.pagination_options.stalled_control_rounds:
                terminal_reason = "enabled_pagination_control_stalled"
                break
            if step.action in {"interaction_failed", "click_failed", "unknown"}:
                terminal_reason = "pagination_interaction_failed"
                break
            previous_height = height
        else:
            terminal_reason = "page_safety_cap_reached"

        unresolved_linkless = sum(candidate.detail_url is None for candidate in selected)
        complete = bool(
            pagination_complete
            and selected
            and not unresolved_linkless
            and not candidate_cap_reached
        )
        if complete:
            reasons = [
                reason
                for reason in reasons
                if reason
                != "Adaptive DOM discovery is bounded and cannot confirm complete pagination."
            ]
            reasons.append(
                "Generalized DOM discovery observed listing exhaustion without persisted selectors."
            )
        elif unresolved_linkless:
            reasons.append(
                f"{unresolved_linkless} linkless candidates remain unresolved across pagination."
            )
        elif terminal_reason:
            reasons.append(
                f"Generalized DOM pagination remained partial: {terminal_reason}."
            )

        metrics = dict(batch.metrics)
        metrics.update(
            {
                "candidates": len(selected),
                "url_candidates": sum(
                    candidate.detail_url is not None for candidate in selected
                ),
                "linkless_candidates": unresolved_linkless,
                "generalized_pagination": {
                    "contract_version": "1.0",
                    "requested_complete": True,
                    "pages_captured": pages_captured,
                    "max_pages": max_pages,
                    "growth_rounds": growth_rounds,
                    "stable_rounds": stable_rounds,
                    "candidate_cap_reached": candidate_cap_reached,
                    "terminal_reason": terminal_reason,
                    "pagination_complete": complete,
                    "actions": actions[:50],
                    "errors": errors[:20],
                },
            }
        )
        return batch.model_copy(
            update={
                "completeness": (
                    CompletenessState.COMPLETE if complete else CompletenessState.PARTIAL
                ),
                "candidates": selected,
                "pages_visited": pages_captured,
                "pagination_complete": complete,
                "metrics": metrics,
                "reasons": list(dict.fromkeys(reasons)),
            }
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

    async def _expand_listing_routes(
        self,
        collector: Any,
        batch: DiscoveryBatch,
        *,
        listing_url: str,
        allowed_hosts: tuple[str, ...],
        max_candidates: int,
    ) -> DiscoveryBatch:
        """Follow at most two evidence-backed category/listing routes.

        This is a generic breadth-one expansion, not recursive crawling.  It is
        used only when the first rendered surface contains no high-confidence
        job-detail URL, and every destination remains inside the caller's
        approved public host scope.
        """

        capture = getattr(collector, "capture", None)
        high_confidence = [
            candidate
            for candidate in batch.candidates
            if candidate.detail_url
            and assess_job_candidate_url(
                candidate.detail_url,
                listing_url=listing_url,
            ).score
            >= 8
        ]
        metrics = {
            "attempted": 0,
            "selected_routes": [],
            "captured_routes": 0,
            "failed_routes": 0,
            "added_candidates": 0,
            "skipped_high_confidence_details_present": bool(high_confidence),
            "available": callable(capture),
        }
        if high_confidence or not callable(capture):
            return self._with_expansion_metrics(batch, metrics)

        routes = self._listing_expansion_candidates(
            batch.candidates,
            listing_url=listing_url,
        )[:2]
        metrics["selected_routes"] = routes
        if not routes:
            return self._with_expansion_metrics(batch, metrics)

        expanded_candidates = list(batch.candidates)
        reasons = list(batch.reasons)
        pages_visited = batch.pages_visited
        before = len(self._select_candidates(expanded_candidates, max_candidates=max_candidates))
        for route in routes:
            metrics["attempted"] += 1
            try:
                report = await capture(route, allowed_hosts=allowed_hosts)
                expanded = await self._discover_report(
                    report,
                    listing_url=route,
                    max_candidates=max_candidates,
                    session=None,
                )
            except Exception as exc:
                metrics["failed_routes"] += 1
                reasons.append(
                    f"Bounded listing expansion failed for {route}: "
                    f"{type(exc).__name__}: {exc}"[:1_000]
                )
                continue
            metrics["captured_routes"] += 1
            pages_visited += expanded.pages_visited
            expanded_candidates.extend(expanded.candidates)
            reasons.extend(expanded.reasons)

        selected = self._select_candidates(
            expanded_candidates,
            max_candidates=max_candidates,
        )
        metrics["added_candidates"] = max(0, len(selected) - before)
        merged_metrics = dict(batch.metrics)
        merged_metrics["listing_expansion"] = metrics
        merged_metrics["candidates"] = len(selected)
        merged_metrics["url_candidates"] = sum(
            candidate.detail_url is not None for candidate in selected
        )
        merged_metrics["linkless_candidates"] = sum(
            candidate.detail_url is None for candidate in selected
        )
        return batch.model_copy(
            update={
                "candidates": selected,
                "pages_visited": pages_visited,
                "pagination_complete": False,
                "completeness": CompletenessState.PARTIAL,
                "metrics": merged_metrics,
                "reasons": list(dict.fromkeys(reasons)),
            }
        )

    @staticmethod
    def _listing_expansion_candidates(
        candidates: Iterable[DiscoveryCandidate],
        *,
        listing_url: str,
    ) -> list[str]:
        listing_host = str(urlsplit(listing_url).hostname or "").lower().rstrip(".")
        ranked: list[tuple[int, float, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            url = str(candidate.detail_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if str(parsed.hostname or "").lower().rstrip(".") != listing_host:
                continue
            assessment = assess_job_candidate_url(url, listing_url=listing_url)
            if assessment.hard_reject or not 0 <= assessment.score < 8:
                continue
            route_text = " ".join(
                (
                    unquote(parsed.path),
                    unquote(parsed.fragment),
                    str(candidate.title_hint or ""),
                )
            )
            if not _LISTING_EXPANSION_ROUTE.search(f"/{route_text.lstrip('/')}"):
                continue
            if _LISTING_EXPANSION_REJECT.search(route_text.replace("-", " ")):
                continue
            origin = str(candidate.evidence.get("origin") or "")
            if origin not in {
                "adaptive_dom_individual_link",
                "adaptive_dom_repeated_cluster",
            }:
                continue
            ranked.append((assessment.score, candidate.confidence, url))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [url for _, _, url in ranked]

    @staticmethod
    def _with_expansion_metrics(
        batch: DiscoveryBatch,
        expansion: dict[str, Any],
    ) -> DiscoveryBatch:
        metrics = dict(batch.metrics)
        metrics["listing_expansion"] = expansion
        return batch.model_copy(update={"metrics": metrics})

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
                -int(
                    candidate.preextracted_job is not None
                    and str(candidate.evidence.get("origin") or "")
                    in {"network_json_record", "inline_json_record"}
                ),
                -int(
                    candidate.detail_url is not None
                    and not bool(candidate.evidence.get("synthetic_detail_identity"))
                ),
                -(
                    assess_job_candidate_url(candidate.detail_url).score
                    if candidate.detail_url is not None
                    else -100
                ),
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
