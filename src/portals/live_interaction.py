from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ..crawl.browser_evidence import BrowserEvidenceSession
from .contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)
from .dom_discovery import DomCandidateDiscoverer
from .job_evidence import is_plausible_job_title
from .rendered_detail import RenderedDetailExtractor, RenderedDetailResult
from .safety import PortalUrlSafetyError, validate_public_http_url


LINKLESS_INTERACTION_CONTRACT_VERSION = "1.2"
_LINKLESS_CLICK_SCRIPT = r"""
(nodeToken) => {
  const map = window.__jobMinerNodeMap;
  const element = map && map.get(String(nodeToken || ''));
  if (!element || !element.isConnected) {
    return {clicked: false, reason: 'node_token_not_live'};
  }
  try { element.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (_) {}
  try {
    element.click();
    return {clicked: true, reason: 'element_click'};
  } catch (error) {
    return {clicked: false, reason: String(error && error.message || error)};
  }
}
"""
_QUERY_DETAIL_KEYS = {
    "detail",
    "id",
    "jid",
    "job",
    "jobid",
    "job_id",
    "posting",
    "postingid",
    "position",
    "positionid",
    "req",
    "reqid",
    "requisition",
    "requisitionid",
}
_QUERY_NAVIGATION_KEYS = {
    "category",
    "department",
    "filter",
    "keyword",
    "location",
    "page",
    "q",
    "query",
    "search",
    "sort",
}


@dataclass(frozen=True)
class LinklessInteractionOptions:
    max_interactions: int = 10
    verification_minimum_confidence: float = 0.62
    settle_time_ms: int = 1_200
    load_state_timeout_ms: int = 8_000

    def __post_init__(self) -> None:
        if not 0 <= self.max_interactions <= 100:
            raise ValueError("max_interactions must be between 0 and 100")
        if not 0.0 <= self.verification_minimum_confidence <= 1.0:
            raise ValueError("verification_minimum_confidence must be between 0 and 1")
        if not 0 <= self.settle_time_ms <= 30_000:
            raise ValueError("settle_time_ms must be between 0 and 30000")
        if not 500 <= self.load_state_timeout_ms <= 60_000:
            raise ValueError("load_state_timeout_ms must be between 500 and 60000")


class LiveLinklessResolver:
    """Resolve grounded linkless cards through live ephemeral node handles.

    Only candidates backed by repeated structural job evidence are clicked.
    The resolver verifies public navigation against the rendered destination.
    Same-URL dialogs may become pre-extracted jobs with a stable, portal-scoped
    fragment identity; no XPath/CSS selector, authenticated state, or challenge
    bypass is used.
    """

    def __init__(
        self,
        *,
        options: LinklessInteractionOptions | None = None,
        discoverer: DomCandidateDiscoverer | None = None,
        rendered_extractor: RenderedDetailExtractor | None = None,
    ) -> None:
        self.options = options or LinklessInteractionOptions()
        self.discoverer = discoverer or DomCandidateDiscoverer()
        self.rendered_extractor = rendered_extractor or RenderedDetailExtractor()

    async def resolve(
        self,
        session: BrowserEvidenceSession,
        candidates: Iterable[DiscoveryCandidate],
        *,
        listing_url: str,
    ) -> DiscoveryBatch:
        raw_candidates = [
            candidate
            for candidate in candidates
            if candidate.kind == DiscoveryCandidateKind.DOM_CLICK
            and candidate.detail_url is None
        ]
        grounded = [
            candidate
            for candidate in raw_candidates
            if bool(candidate.evidence.get("structural_job_grounding"))
            and bool(candidate.evidence.get("evidence_preserving"))
        ]
        grounded_ids = {candidate.candidate_id for candidate in grounded}
        verification_only = [
            candidate
            for candidate in raw_candidates
            if candidate.candidate_id not in grounded_ids
            and candidate.confidence >= self.options.verification_minimum_confidence
            and bool(candidate.evidence.get("page_job_context"))
            and str(candidate.evidence.get("origin") or "")
            == "adaptive_dom_repeated_cluster"
            and is_plausible_job_title(candidate.title_hint)
        ]
        eligible = [*grounded, *verification_only]
        verification_only_ids = {
            candidate.candidate_id for candidate in verification_only
        }
        bounded = eligible[: self.options.max_interactions]
        resolved: list[DiscoveryCandidate] = []
        resolved_source_ids: list[str] = []
        errors: list[str] = []
        metrics: dict[str, Any] = {
            "contract_version": LINKLESS_INTERACTION_CONTRACT_VERSION,
            "linkless_candidates": len(raw_candidates),
            "eligible_candidates": len(eligible),
            "grounded_candidates": len(grounded),
            "verification_only_candidates": len(verification_only),
            "ineligible_candidates": len(raw_candidates) - len(eligible),
            "attempted": 0,
            "resolved": 0,
            "navigations": 0,
            "popups": 0,
            "unchanged_or_modal": 0,
            "modal_extractions": 0,
            "verified_details": 0,
            "unverified_destinations": 0,
            "verification_unavailable": 0,
            "stale_tokens": 0,
            "unsafe_destinations": 0,
            "interaction_network_responses": 0,
            "interaction_network_errors": 0,
            "structured_network_extractions": 0,
            "bounded": len(eligible) > len(bounded),
        }
        current_report = session.report

        for position, original in enumerate(bounded):
            requires_rendered_verification = original.candidate_id in verification_only_ids
            if position:
                try:
                    current_report = await session.collector.capture_page(
                        session.page,
                        listing_url,
                        allowed_hosts=session.allowed_hosts,
                    )
                except Exception as exc:
                    errors.append(
                        f"listing reset failed after {position} interactions: "
                        f"{type(exc).__name__}: {exc}"[:1_000]
                    )
                    break
            current_batch = self.discoverer.discover(
                current_report,
                listing_url=current_report.final_url,
            )
            live_candidate = self._match_candidate(original, current_batch.linkless_candidates)
            if live_candidate is None:
                metrics["stale_tokens"] += 1
                errors.append(f"{original.candidate_id}: live structural record was not found")
                continue

            frame = self._frame_for_candidate(session.page, live_candidate)
            if frame is None or not live_candidate.node_token:
                metrics["stale_tokens"] += 1
                errors.append(f"{original.candidate_id}: approved live frame/token was not found")
                continue

            metrics["attempted"] += 1
            pages_before = list(getattr(session.context, "pages", []) or [])
            interaction_network: list[Any] = []
            interaction_network_errors: list[str] = []

            async def perform_click() -> tuple[Any, str | None]:
                click_error: str | None = None
                clicked: Any = None
                try:
                    clicked = await frame.evaluate(
                        _LINKLESS_CLICK_SCRIPT,
                        live_candidate.node_token,
                    )
                except Exception as exc:
                    # Immediate navigation can destroy the JavaScript execution
                    # context before evaluate() returns. Treat that as ambiguous
                    # until the observable page/popup URL is checked below.
                    click_error = f"{type(exc).__name__}: {exc}"[:500]
                if self.options.settle_time_ms and (
                    click_error is not None
                    or (
                        isinstance(clicked, dict)
                        and bool(clicked.get("clicked"))
                    )
                ):
                    await asyncio.sleep(self.options.settle_time_ms / 1_000.0)
                return clicked, click_error

            capture_network = getattr(
                session.collector,
                "capture_interaction_network",
                None,
            )
            try:
                if callable(capture_network):
                    (
                        (clicked, click_error),
                        interaction_network,
                        interaction_network_errors,
                    ) = await capture_network(
                        session.context,
                        perform_click,
                        allowed_hosts=session.allowed_hosts,
                    )
                else:
                    clicked, click_error = await perform_click()
            except Exception as exc:
                clicked = None
                click_error = f"{type(exc).__name__}: {exc}"[:500]
            if clicked is not None and (
                not isinstance(clicked, dict) or not bool(clicked.get("clicked"))
            ):
                metrics["stale_tokens"] += 1
                errors.append(
                    f"{original.candidate_id}: "
                    f"{str((clicked or {}).get('reason') if isinstance(clicked, dict) else clicked)[:300]}"
                )
                continue
            metrics["interaction_network_responses"] += len(interaction_network)
            metrics["interaction_network_errors"] += len(interaction_network_errors)
            errors.extend(
                f"{original.candidate_id}: {message}"[:1_000]
                for message in interaction_network_errors[:5]
            )

            pages_after = list(getattr(session.context, "pages", []) or [])
            popup_pages = [page for page in pages_after if page not in pages_before]
            destination_page = popup_pages[-1] if popup_pages else session.page
            interaction_kind = "popup" if popup_pages else "navigation"
            if popup_pages:
                metrics["popups"] += 1
            try:
                await destination_page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=self.options.load_state_timeout_ms,
                )
            except Exception:
                pass
            observed_url = str(getattr(destination_page, "url", "") or "")
            try:
                checked = validate_public_http_url(
                    observed_url,
                    allowed_hosts=session.allowed_hosts,
                )
            except (PortalUrlSafetyError, ValueError) as exc:
                metrics["unsafe_destinations"] += 1
                errors.append(
                    f"{original.candidate_id}: destination rejected: {type(exc).__name__}: {exc}"[:1_000]
                )
                await self._close_popups(popup_pages)
                continue

            rendered = await self._rendered_destination(
                session,
                destination_page,
                requested_url=checked.normalized_url,
                baseline_report=current_report,
                original=original,
                metrics=metrics,
                errors=errors,
                interaction_network=interaction_network,
                interaction_network_errors=interaction_network_errors,
            )
            if (
                rendered is not None
                and rendered.job is not None
                and rendered.metrics.get("strategy") == "structured_json"
                and interaction_network
            ):
                metrics["structured_network_extractions"] += 1
            openable_route = self._is_openable_detail_route(
                listing_url,
                checked.normalized_url,
                source_job_id=original.source_job_id,
            )
            if not openable_route:
                if rendered is not None and rendered.job is not None:
                    modal_candidate = self._resolved_modal_candidate(
                        original,
                        live_candidate,
                        listing_url=listing_url,
                        rendered=rendered,
                    )
                    resolved.append(modal_candidate)
                    resolved_source_ids.append(original.candidate_id)
                    metrics["resolved"] += 1
                    metrics["modal_extractions"] += 1
                    metrics["verified_details"] += 1
                    await self._close_popups(popup_pages)
                    continue
                metrics["unchanged_or_modal"] += 1
                errors.append(
                    f"{original.candidate_id}: click exposed neither a unique verified detail "
                    "URL nor an extractable rendered modal"
                    + (f" ({click_error})" if click_error else "")
                )
                await self._close_popups(popup_pages)
                continue

            # With the real collector, navigation alone is not job evidence.
            # A marketing/category destination must not be preserved simply
            # because a structurally repeated card opened it.
            if rendered is not None and rendered.job is None:
                metrics["unverified_destinations"] += 1
                errors.append(
                    f"{original.candidate_id}: destination failed rendered job verification "
                    f"({rendered.reason})"
                )
                await self._close_popups(popup_pages)
                continue
            if requires_rendered_verification and (
                rendered is None or rendered.job is None
            ):
                metrics["unverified_destinations"] += 1
                errors.append(
                    f"{original.candidate_id}: weak structural candidate did not produce "
                    "a strictly verified rendered job"
                )
                await self._close_popups(popup_pages)
                continue

            if not popup_pages:
                metrics["navigations"] += 1
            if rendered is not None and rendered.job is not None:
                metrics["verified_details"] += 1
            resolved.append(
                self._resolved_candidate(
                    original,
                    live_candidate,
                    detail_url=checked.normalized_url,
                    interaction_kind=interaction_kind,
                    preextracted_job=rendered.job if rendered is not None else None,
                )
            )
            resolved_source_ids.append(original.candidate_id)
            metrics["resolved"] += 1
            await self._close_popups(popup_pages)

        metrics["resolved_source_candidate_ids"] = resolved_source_ids
        reasons = [
            "Live linkless interaction is bounded and cannot confirm complete pagination."
        ]
        reasons.extend(errors[:20])
        return DiscoveryBatch(
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
            completeness=CompletenessState.PARTIAL,
            candidates=self._deduplicate(resolved),
            pages_visited=max(1, int(metrics["attempted"])),
            pagination_complete=False,
            reasons=reasons,
            metrics=metrics,
        )

    async def _rendered_destination(
        self,
        session: BrowserEvidenceSession,
        page: Any,
        *,
        requested_url: str,
        baseline_report: Any,
        original: DiscoveryCandidate,
        metrics: dict[str, Any],
        errors: list[str],
        interaction_network: list[Any],
        interaction_network_errors: list[str],
    ) -> RenderedDetailResult | None:
        snapshot = getattr(session.collector, "snapshot_current_page", None)
        if not callable(snapshot):
            metrics["verification_unavailable"] += 1
            return None
        try:
            report = await snapshot(
                page,
                requested_url,
                allowed_hosts=session.allowed_hosts,
            )
        except Exception as exc:
            metrics["unverified_destinations"] += 1
            errors.append(
                f"{original.candidate_id}: post-click snapshot failed: "
                f"{type(exc).__name__}: {exc}"[:1_000]
            )
            return RenderedDetailResult(
                job=None,
                reason="post_click_snapshot_failed",
                metrics={"error_type": type(exc).__name__},
            )
        if interaction_network:
            report_metrics = dict(report.metrics)
            report_metrics.update(
                {
                    "snapshot_kind": "post_interaction_dom_and_network",
                    "network_json_responses": len(interaction_network),
                }
            )
            report = report.model_copy(
                update={
                    "network_json": interaction_network,
                    "errors": [*report.errors, *interaction_network_errors],
                    "metrics": report_metrics,
                }
            )
        baseline_url = str(getattr(baseline_report, "final_url", "") or "")
        same_document = requested_url.rstrip("/") == baseline_url.rstrip("/")
        return self.rendered_extractor.extract(
            report,
            fallback_url=report.final_url,
            title_hint=original.title_hint,
            location_hint=original.location_hint,
            # DOM deltas are meaningful for same-URL dialogs. A different URL
            # is a complete destination document and must meet the stronger
            # non-modal detail threshold on its own.
            baseline_report=baseline_report if same_document else None,
        )

    @staticmethod
    def _frame_for_candidate(page: Any, candidate: DiscoveryCandidate) -> Any | None:
        frames = list(getattr(page, "frames", []) or [])
        frame_id = str(candidate.evidence.get("frame_id") or "")
        if frame_id.startswith("f") and frame_id[1:].isdigit():
            index = int(frame_id[1:])
            if 0 <= index < len(frames):
                return frames[index]
        candidate_frame_url = str(candidate.frame_url or candidate.evidence.get("frame_url") or "")
        for frame in frames:
            if str(getattr(frame, "url", "") or "") == candidate_frame_url:
                return frame
        return None

    @staticmethod
    def _match_candidate(
        original: DiscoveryCandidate,
        live_candidates: Iterable[DiscoveryCandidate],
    ) -> DiscoveryCandidate | None:
        values = list(live_candidates)
        exact = next(
            (candidate for candidate in values if candidate.candidate_id == original.candidate_id),
            None,
        )
        if exact is not None:
            return exact

        def score(candidate: DiscoveryCandidate) -> int:
            value = 0
            if original.source_job_id and candidate.source_job_id == original.source_job_id:
                value += 10
            if original.title_hint and candidate.title_hint == original.title_hint:
                value += 4
            if original.location_hint and candidate.location_hint == original.location_hint:
                value += 2
            if (
                original.evidence.get("cluster_signature")
                and candidate.evidence.get("cluster_signature")
                == original.evidence.get("cluster_signature")
            ):
                value += 1
            return value

        ranked = sorted(((score(candidate), candidate) for candidate in values), key=lambda item: -item[0])
        return ranked[0][1] if ranked and ranked[0][0] >= 5 else None

    @staticmethod
    def _is_openable_detail_route(
        listing_url: str,
        candidate_url: str,
        *,
        source_job_id: str | None,
    ) -> bool:
        listing = urlsplit(listing_url)
        candidate = urlsplit(candidate_url)
        if candidate_url.rstrip("/") == listing_url.rstrip("/"):
            return False
        same_document_path = (
            listing.scheme.lower(),
            (listing.hostname or "").lower(),
            listing.path.rstrip("/") or "/",
        ) == (
            candidate.scheme.lower(),
            (candidate.hostname or "").lower(),
            candidate.path.rstrip("/") or "/",
        )
        if not same_document_path:
            return True
        if candidate.fragment and candidate.fragment != listing.fragment:
            return True
        query = {key.lower(): value for key, value in parse_qsl(candidate.query)}
        listing_query = {key.lower(): value for key, value in parse_qsl(listing.query)}
        changed = {key: value for key, value in query.items() if listing_query.get(key) != value}
        if not changed:
            return False
        if set(changed) <= _QUERY_NAVIGATION_KEYS:
            return False
        if set(changed) & _QUERY_DETAIL_KEYS:
            return True
        normalized_id = str(source_job_id or "").strip().lower()
        return bool(normalized_id and normalized_id in " ".join(changed.values()).lower())

    @staticmethod
    def _resolved_candidate(
        original: DiscoveryCandidate,
        live_candidate: DiscoveryCandidate,
        *,
        detail_url: str,
        interaction_kind: str,
        preextracted_job: Any | None = None,
    ) -> DiscoveryCandidate:
        evidence = {
            "origin": "adaptive_dom_linkless_interaction",
            "contract_version": LINKLESS_INTERACTION_CONTRACT_VERSION,
            "source_candidate_id": original.candidate_id,
            "interaction_kind": interaction_kind,
            "source_structural_job_grounding": bool(
                original.evidence.get("structural_job_grounding")
            ),
            "evidence_preserving": bool(
                original.evidence.get("evidence_preserving")
                or preextracted_job is not None
            ),
            "cluster_signature": original.evidence.get("cluster_signature"),
            "rendered_detail_verified": preextracted_job is not None,
            "live_node_token_hash": hashlib.sha256(
                str(live_candidate.node_token or "").encode("utf-8")
            ).hexdigest()[:16],
        }
        candidate = DiscoveryCandidate.from_url(
            detail_url,
            source_job_id=original.source_job_id,
            confidence=min(0.99, max(original.confidence, live_candidate.confidence) + 0.03),
            evidence=evidence,
            preextracted_job=(
                preextracted_job.model_copy(update={"job_url": detail_url})
                if preextracted_job is not None
                else None
            ),
        )
        return candidate.model_copy(
            update={
                "title_hint": original.title_hint or live_candidate.title_hint,
                "location_hint": original.location_hint or live_candidate.location_hint,
            }
        )

    @staticmethod
    def _resolved_modal_candidate(
        original: DiscoveryCandidate,
        live_candidate: DiscoveryCandidate,
        *,
        listing_url: str,
        rendered: RenderedDetailResult,
    ) -> DiscoveryCandidate:
        assert rendered.job is not None
        source_identity = (
            original.source_job_id
            or rendered.job.job_reference
            or hashlib.sha256(
                "|".join(
                    (
                        str(rendered.job.title or original.title_hint or ""),
                        str(rendered.job.location_text or original.location_hint or ""),
                    )
                ).encode("utf-8")
            ).hexdigest()[:20]
        )
        safe_identity = re.sub(r"[^A-Za-z0-9._-]+", "-", str(source_identity)).strip("-")
        safe_identity = safe_identity[:120] or hashlib.sha256(
            original.candidate_id.encode("utf-8")
        ).hexdigest()[:20]
        parts = urlsplit(listing_url)
        detail_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path or "/", parts.query, f"job/{safe_identity}")
        )
        evidence = {
            "origin": "adaptive_dom_linkless_interaction",
            "contract_version": LINKLESS_INTERACTION_CONTRACT_VERSION,
            "source_candidate_id": original.candidate_id,
            "interaction_kind": "same_url_modal",
            "source_structural_job_grounding": bool(
                original.evidence.get("structural_job_grounding")
            ),
            "evidence_preserving": True,
            "rendered_detail_verified": True,
            "synthetic_detail_identity": True,
            "cluster_signature": original.evidence.get("cluster_signature"),
            "rendered_detail_metrics": dict(rendered.metrics),
            "live_node_token_hash": hashlib.sha256(
                str(live_candidate.node_token or "").encode("utf-8")
            ).hexdigest()[:16],
        }
        job = rendered.job.model_copy(
            update={
                "job_url": detail_url,
                "apply_url": rendered.job.apply_url or listing_url,
                "job_reference": rendered.job.job_reference or original.source_job_id,
            }
        )
        digest = hashlib.sha256(
            f"{listing_url}|{source_identity}".encode("utf-8")
        ).hexdigest()[:24]
        return DiscoveryCandidate(
            candidate_id=f"modal_{digest}",
            kind=DiscoveryCandidateKind.MODAL,
            detail_url=detail_url,
            apply_url=job.apply_url,
            source_job_id=job.job_reference or original.source_job_id,
            title_hint=job.title or original.title_hint,
            location_hint=job.location_text or original.location_hint,
            node_token=live_candidate.node_token or original.node_token,
            frame_url=live_candidate.frame_url or original.frame_url,
            confidence=max(0.90, rendered.confidence),
            evidence=evidence,
            preextracted_job=job,
        )

    @staticmethod
    async def _close_popups(pages: Iterable[Any]) -> None:
        for page in pages:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    def _deduplicate(candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
        selected: dict[str, DiscoveryCandidate] = {}
        for candidate in candidates:
            existing = selected.get(str(candidate.detail_url))
            if existing is None or candidate.confidence > existing.confidence:
                selected[str(candidate.detail_url)] = candidate
        return list(selected.values())
