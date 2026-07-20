from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

from ..crawl.browser_evidence import BrowserEvidenceSession
from .contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)
from .dom_discovery import DomCandidateDiscoverer
from .safety import PortalUrlSafetyError, validate_public_http_url


LINKLESS_INTERACTION_CONTRACT_VERSION = "1.0"
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
    settle_time_ms: int = 1_200
    load_state_timeout_ms: int = 8_000

    def __post_init__(self) -> None:
        if not 0 <= self.max_interactions <= 100:
            raise ValueError("max_interactions must be between 0 and 100")
        if not 0 <= self.settle_time_ms <= 30_000:
            raise ValueError("settle_time_ms must be between 0 and 30000")
        if not 500 <= self.load_state_timeout_ms <= 60_000:
            raise ValueError("load_state_timeout_ms must be between 500 and 60000")


class LiveLinklessResolver:
    """Resolve grounded linkless cards through live ephemeral node handles.

    Only candidates backed by repeated structural job evidence are clicked.
    The resolver observes public navigation or a popup URL; it never invents a
    URL, persists a selector, imports cookies, solves a challenge, or treats an
    unchanged modal listing URL as a unique job identity.
    """

    def __init__(
        self,
        *,
        options: LinklessInteractionOptions | None = None,
        discoverer: DomCandidateDiscoverer | None = None,
    ) -> None:
        self.options = options or LinklessInteractionOptions()
        self.discoverer = discoverer or DomCandidateDiscoverer()

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
        eligible = [
            candidate
            for candidate in raw_candidates
            if bool(candidate.evidence.get("structural_job_grounding"))
            and bool(candidate.evidence.get("evidence_preserving"))
        ]
        bounded = eligible[: self.options.max_interactions]
        resolved: list[DiscoveryCandidate] = []
        resolved_source_ids: list[str] = []
        errors: list[str] = []
        metrics: dict[str, Any] = {
            "contract_version": LINKLESS_INTERACTION_CONTRACT_VERSION,
            "linkless_candidates": len(raw_candidates),
            "eligible_candidates": len(eligible),
            "ineligible_candidates": len(raw_candidates) - len(eligible),
            "attempted": 0,
            "resolved": 0,
            "navigations": 0,
            "popups": 0,
            "unchanged_or_modal": 0,
            "stale_tokens": 0,
            "unsafe_destinations": 0,
            "bounded": len(eligible) > len(bounded),
        }
        current_report = session.report

        for position, original in enumerate(bounded):
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
            click_error: str | None = None
            try:
                clicked = await frame.evaluate(
                    _LINKLESS_CLICK_SCRIPT,
                    live_candidate.node_token,
                )
                if not isinstance(clicked, dict) or not bool(clicked.get("clicked")):
                    metrics["stale_tokens"] += 1
                    errors.append(
                        f"{original.candidate_id}: "
                        f"{str((clicked or {}).get('reason') if isinstance(clicked, dict) else clicked)[:300]}"
                    )
                    continue
            except Exception as exc:
                # Immediate navigation can destroy the JavaScript execution
                # context before evaluate() returns. Treat that as ambiguous
                # until the observable page/popup URL is checked below.
                click_error = f"{type(exc).__name__}: {exc}"[:500]
            if self.options.settle_time_ms:
                await asyncio.sleep(self.options.settle_time_ms / 1_000.0)

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

            if not self._is_openable_detail_route(
                listing_url,
                checked.normalized_url,
                source_job_id=original.source_job_id,
            ):
                metrics["unchanged_or_modal"] += 1
                errors.append(
                    f"{original.candidate_id}: click did not expose a unique public detail URL"
                    + (f" ({click_error})" if click_error else "")
                )
                await self._close_popups(popup_pages)
                continue

            if not popup_pages:
                metrics["navigations"] += 1
            resolved.append(
                self._resolved_candidate(
                    original,
                    live_candidate,
                    detail_url=checked.normalized_url,
                    interaction_kind=interaction_kind,
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
    ) -> DiscoveryCandidate:
        evidence = {
            "origin": "adaptive_dom_linkless_interaction",
            "contract_version": LINKLESS_INTERACTION_CONTRACT_VERSION,
            "source_candidate_id": original.candidate_id,
            "interaction_kind": interaction_kind,
            "source_structural_job_grounding": bool(
                original.evidence.get("structural_job_grounding")
            ),
            "evidence_preserving": bool(original.evidence.get("evidence_preserving")),
            "cluster_signature": original.evidence.get("cluster_signature"),
            "live_node_token_hash": hashlib.sha256(
                str(live_candidate.node_token or "").encode("utf-8")
            ).hexdigest()[:16],
        }
        candidate = DiscoveryCandidate.from_url(
            detail_url,
            source_job_id=original.source_job_id,
            confidence=min(0.99, max(original.confidence, live_candidate.confidence) + 0.03),
            evidence=evidence,
        )
        return candidate.model_copy(
            update={
                "title_hint": original.title_hint or live_candidate.title_hint,
                "location_hint": original.location_hint or live_candidate.location_hint,
            }
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
