from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Iterable
from urllib.parse import urlsplit

from ..crawl.browser_evidence import BrowserEvidenceReport
from ..crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from .contracts import (
    CompletenessState,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateKind,
    ScrapeStrategy,
)


DOM_DISCOVERY_CONTRACT_VERSION = "1.0"
_PAGE_JOB_CONTEXT = re.compile(
    r"\b(job|jobs|career|careers|vacanc(?:y|ies)|position|positions|"
    r"opportunit(?:y|ies)|opening|openings|employment)\b",
    re.I,
)
_ROW_JOB_EVIDENCE = re.compile(
    r"\b(location|remote|hybrid|salary|compensation|posted|apply|"
    r"view\s+(?:job|role|position)|job\s+details?|position\s+details?|"
    r"full[- ]?time|part[- ]?time|contract|department|requisition|"
    r"vacanc(?:y|ies)|opening|openings)\b",
    re.I,
)
_NAVIGATION_TEXT = re.compile(
    r"^(home|about(?: us)?|contact(?: us)?|privacy|terms|resources|news|"
    r"blog|login|sign in|register|cookie settings|skip to main content|"
    r"previous|next|back|menu|search)$",
    re.I,
)
_DATE_TEXT = re.compile(
    r"^(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|"
    r"\d+\s+(?:minute|hour|day|week|month)s?\s+ago)$",
    re.I,
)
_EXCLUDED_ANCESTOR_TAGS = {"header", "footer", "nav"}
_EXCLUDED_ANCESTOR_ROLES = {"navigation", "contentinfo", "banner"}
_INTERACTIVE_FORM_TAGS = {"input", "select", "textarea", "option"}


@dataclass(frozen=True)
class DomDiscoveryOptions:
    minimum_cluster_size: int = 3
    minimum_confidence: float = 0.62
    evidence_preservation_confidence: float = 0.74
    max_candidates: int = 1_000
    max_descendants_per_root: int = 250

    def __post_init__(self) -> None:
        if not 2 <= self.minimum_cluster_size <= 50:
            raise ValueError("minimum_cluster_size must be between 2 and 50")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if not self.minimum_confidence <= self.evidence_preservation_confidence <= 1.0:
            raise ValueError(
                "evidence_preservation_confidence must be at least minimum_confidence"
            )
        if not 1 <= self.max_candidates <= 10_000:
            raise ValueError("max_candidates must be between 1 and 10000")


@dataclass
class _FrameIndex:
    frame: FrameDomSnapshot
    by_token: dict[str, DomNodeEvidence]
    children: dict[str | None, list[DomNodeEvidence]]

    @classmethod
    def build(cls, frame: FrameDomSnapshot) -> "_FrameIndex":
        by_token = {node.node_token: node for node in frame.nodes}
        children: dict[str | None, list[DomNodeEvidence]] = defaultdict(list)
        for node in frame.nodes:
            children[node.parent_token].append(node)
        return cls(frame=frame, by_token=by_token, children=dict(children))

    def descendants(self, root: DomNodeEvidence, *, limit: int) -> list[DomNodeEvidence]:
        values: list[DomNodeEvidence] = []
        queue = list(self.children.get(root.node_token, []))
        while queue and len(values) < limit:
            node = queue.pop(0)
            values.append(node)
            queue.extend(self.children.get(node.node_token, []))
        return values

    def excluded_by_ancestry(self, node: DomNodeEvidence) -> bool:
        current: DomNodeEvidence | None = node
        for _ in range(30):
            if current is None:
                break
            if current.tag in _EXCLUDED_ANCESTOR_TAGS:
                return True
            if current.role in _EXCLUDED_ANCESTOR_ROLES:
                return True
            current = self.by_token.get(current.parent_token or "")
        return False


@dataclass(frozen=True)
class _RootEvidence:
    root: DomNodeEvidence
    nodes: tuple[DomNodeEvidence, ...]
    text_values: tuple[str, ...]
    primary_url: str | None
    click_node: DomNodeEvidence | None
    title_hint: str | None
    location_hint: str | None
    source_job_id: str | None

    @property
    def identity(self) -> str:
        return self.primary_url or self.source_job_id or "|".join(self.text_values[:3])


class DomCandidateDiscoverer:
    """Infer repeated job records from DOM structure without XPath or URL patterns."""

    def __init__(self, options: DomDiscoveryOptions | None = None) -> None:
        self.options = options or DomDiscoveryOptions()

    def discover(
        self,
        report: BrowserEvidenceReport,
        *,
        listing_url: str | None = None,
    ) -> DiscoveryBatch:
        requested_url = str(listing_url or report.final_url)
        page_context = " ".join(
            [
                str(report.title or ""),
                *(str(frame.title or "") for frame in report.frames),
                *(
                    str(node.text or "")
                    for frame in report.frames
                    for node in frame.nodes[:80]
                    if node.role in {"heading", "main"}
                ),
            ]
        )
        page_has_job_context = bool(_PAGE_JOB_CONTEXT.search(page_context))
        candidates: list[DiscoveryCandidate] = []
        metrics = {
            "contract_version": DOM_DISCOVERY_CONTRACT_VERSION,
            "frames_scanned": 0,
            "nodes_scanned": 0,
            "clusters_scanned": 0,
            "clusters_qualified": 0,
            "cluster_candidates": 0,
            "individual_link_candidates": 0,
            "page_job_context": page_has_job_context,
        }

        for frame in report.frames:
            if frame.error or not frame.nodes:
                continue
            metrics["frames_scanned"] += 1
            metrics["nodes_scanned"] += len(frame.nodes)
            index = _FrameIndex.build(frame)
            frame_candidates, frame_metrics = self._discover_repeated_clusters(
                index,
                page_has_job_context=page_has_job_context,
            )
            candidates.extend(frame_candidates)
            metrics["clusters_scanned"] += frame_metrics["clusters_scanned"]
            metrics["clusters_qualified"] += frame_metrics["clusters_qualified"]
            metrics["cluster_candidates"] += len(frame_candidates)

            represented_urls = {
                candidate.detail_url for candidate in candidates if candidate.detail_url
            }
            individual = self._discover_individual_links(
                index,
                page_has_job_context=page_has_job_context,
                represented_urls=represented_urls,
                listing_url=requested_url,
            )
            candidates.extend(individual)
            metrics["individual_link_candidates"] += len(individual)

        deduplicated = self._deduplicate(candidates)[: self.options.max_candidates]
        metrics.update(
            {
                "candidates_before_deduplication": len(candidates),
                "candidates": len(deduplicated),
                "url_candidates": sum(candidate.detail_url is not None for candidate in deduplicated),
                "linkless_candidates": sum(candidate.detail_url is None for candidate in deduplicated),
                "high_confidence_candidates": sum(
                    candidate.confidence >= self.options.evidence_preservation_confidence
                    for candidate in deduplicated
                ),
                "bounded": bool(len(candidates) > self.options.max_candidates),
            }
        )
        reasons = [
            "Adaptive DOM discovery is bounded and cannot confirm complete pagination."
        ]
        if not deduplicated:
            reasons.append("No repeated or semantically grounded DOM job records were found.")
        return DiscoveryBatch(
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
            completeness=CompletenessState.PARTIAL,
            candidates=deduplicated,
            pages_visited=1,
            pagination_complete=False,
            reasons=reasons,
            metrics=metrics,
        )

    def _discover_repeated_clusters(
        self,
        index: _FrameIndex,
        *,
        page_has_job_context: bool,
    ) -> tuple[list[DiscoveryCandidate], dict[str, int]]:
        candidates: list[DiscoveryCandidate] = []
        clusters_scanned = 0
        clusters_qualified = 0
        for siblings in index.children.values():
            grouped: dict[str, list[DomNodeEvidence]] = defaultdict(list)
            for node in siblings:
                grouped[node.structural_signature].append(node)
            for signature, roots in grouped.items():
                if not signature or len(roots) < self.options.minimum_cluster_size:
                    continue
                clusters_scanned += 1
                usable_roots = [root for root in roots if not index.excluded_by_ancestry(root)]
                if len(usable_roots) < self.options.minimum_cluster_size:
                    continue
                evidence_rows = [self._root_evidence(index, root) for root in usable_roots]
                evidence_rows = [row for row in evidence_rows if row is not None]
                if len(evidence_rows) < self.options.minimum_cluster_size:
                    continue

                unique_identity_ratio = len({row.identity for row in evidence_rows}) / len(
                    evidence_rows
                )
                median_fields = median(len(row.text_values) for row in evidence_rows)
                group_text = " ".join(
                    value for row in evidence_rows[:20] for value in row.text_values[:8]
                )
                row_has_job_evidence = bool(_ROW_JOB_EVIDENCE.search(group_text))
                table_rows = all(row.root.tag == "tr" for row in evidence_rows)
                has_openable_identity = sum(
                    bool(row.primary_url or row.click_node) for row in evidence_rows
                ) >= self.options.minimum_cluster_size
                if not has_openable_identity or unique_identity_ratio < 0.5:
                    continue
                if table_rows:
                    structurally_grounded = median_fields >= 2
                else:
                    structurally_grounded = (
                        median_fields >= 2
                        and (page_has_job_context or row_has_job_evidence)
                    )
                if not structurally_grounded:
                    continue

                confidence = self._cluster_confidence(
                    cluster_size=len(evidence_rows),
                    unique_identity_ratio=unique_identity_ratio,
                    median_fields=float(median_fields),
                    table_rows=table_rows,
                    page_has_job_context=page_has_job_context,
                    row_has_job_evidence=row_has_job_evidence,
                )
                if confidence < self.options.minimum_confidence:
                    continue
                preservation_eligible = (
                    page_has_job_context
                    and unique_identity_ratio >= 0.75
                    and (
                        (table_rows and median_fields >= 2)
                        or (row_has_job_evidence and median_fields >= 3)
                    )
                )
                clusters_qualified += 1
                for row_index, row in enumerate(evidence_rows):
                    candidate = self._candidate_from_root(
                        index,
                        row,
                        confidence=confidence,
                        cluster_signature=signature,
                        cluster_size=len(evidence_rows),
                        row_index=row_index,
                        unique_identity_ratio=unique_identity_ratio,
                        median_fields=float(median_fields),
                        table_rows=table_rows,
                        page_has_job_context=page_has_job_context,
                        row_has_job_evidence=row_has_job_evidence,
                        preservation_eligible=preservation_eligible,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
        return candidates, {
            "clusters_scanned": clusters_scanned,
            "clusters_qualified": clusters_qualified,
        }

    def _root_evidence(
        self,
        index: _FrameIndex,
        root: DomNodeEvidence,
    ) -> _RootEvidence | None:
        nodes = [
            root,
            *index.descendants(root, limit=self.options.max_descendants_per_root),
        ]
        visible_nodes = [node for node in nodes if node.visible]
        text_values = tuple(
            dict.fromkeys(
                text
                for node in visible_nodes
                if (text := self._useful_text(node.text)) is not None
            )
        )
        urls = list(
            dict.fromkeys(
                node.href for node in visible_nodes if node.href and node.clickable
            )
        )
        primary_url = self._select_primary_url(visible_nodes, urls)
        linkless_clickables = [
            node
            for node in visible_nodes
            if node.clickable
            and not node.href
            and node.tag not in _INTERACTIVE_FORM_TAGS
            and node.role not in {"textbox", "combobox", "checkbox", "radio", "option"}
        ]
        click_node = linkless_clickables[0] if linkless_clickables else None
        if not primary_url and click_node is None:
            return None
        title_hint = self._select_title(visible_nodes, primary_url)
        location_hint = self._select_location(text_values, title_hint)
        source_job_id = self._source_job_id(visible_nodes)
        return _RootEvidence(
            root=root,
            nodes=tuple(visible_nodes),
            text_values=text_values,
            primary_url=primary_url,
            click_node=click_node,
            title_hint=title_hint,
            location_hint=location_hint,
            source_job_id=source_job_id,
        )

    def _candidate_from_root(
        self,
        index: _FrameIndex,
        row: _RootEvidence,
        *,
        confidence: float,
        cluster_signature: str,
        cluster_size: int,
        row_index: int,
        unique_identity_ratio: float,
        median_fields: float,
        table_rows: bool,
        page_has_job_context: bool,
        row_has_job_evidence: bool,
        preservation_eligible: bool,
    ) -> DiscoveryCandidate | None:
        evidence = {
            "origin": "adaptive_dom_repeated_cluster",
            "contract_version": DOM_DISCOVERY_CONTRACT_VERSION,
            "frame_id": index.frame.frame_id,
            "frame_url": index.frame.frame_url,
            "root_node_token": row.root.node_token,
            "interaction_node_token": row.click_node.node_token if row.click_node else None,
            "cluster_signature": cluster_signature,
            "cluster_size": cluster_size,
            "row_index": row_index,
            "unique_identity_ratio": round(unique_identity_ratio, 4),
            "median_fields": round(median_fields, 4),
            "table_rows": table_rows,
            "page_job_context": page_has_job_context,
            "row_job_evidence": row_has_job_evidence,
            "structural_job_grounding": preservation_eligible,
            "row_text": list(row.text_values[:12]),
            "evidence_preserving": (
                preservation_eligible
                and confidence >= self.options.evidence_preservation_confidence
            ),
        }
        if row.primary_url:
            candidate = DiscoveryCandidate.from_url(
                row.primary_url,
                source_job_id=row.source_job_id,
                confidence=confidence,
                evidence=evidence,
            )
            return candidate.model_copy(
                update={
                    "title_hint": row.title_hint,
                    "location_hint": row.location_hint,
                }
            )
        if row.click_node is None:
            return None
        stable_identity = "|".join(
            (
                index.frame.frame_url,
                cluster_signature,
                row.source_job_id or "",
                row.title_hint or "",
                row.location_hint or "",
            )
        )
        digest = hashlib.sha256(stable_identity.encode("utf-8")).hexdigest()[:24]
        return DiscoveryCandidate(
            candidate_id=f"dom_click_{digest}",
            kind=DiscoveryCandidateKind.DOM_CLICK,
            source_job_id=row.source_job_id,
            title_hint=row.title_hint,
            location_hint=row.location_hint,
            node_token=row.click_node.node_token,
            frame_url=index.frame.frame_url,
            confidence=confidence,
            evidence=evidence,
        )

    def _discover_individual_links(
        self,
        index: _FrameIndex,
        *,
        page_has_job_context: bool,
        represented_urls: set[str],
        listing_url: str,
    ) -> list[DiscoveryCandidate]:
        candidates: list[DiscoveryCandidate] = []
        for node in index.frame.nodes:
            if not node.clickable or not node.href or node.href in represented_urls:
                continue
            if node.href.rstrip("/") == listing_url.rstrip("/"):
                continue
            if self._same_document_route(node.href, listing_url):
                continue
            if index.excluded_by_ancestry(node):
                continue
            text = self._useful_text(node.text)
            if not text or _NAVIGATION_TEXT.fullmatch(text):
                continue
            attributes = " ".join(f"{key} {value}" for key, value in node.attributes.items())
            local_job_context = bool(_PAGE_JOB_CONTEXT.search(f"{text} {attributes}"))
            parent = index.by_token.get(node.parent_token or "")
            parent_repeated = bool(
                parent
                and sum(
                    sibling.structural_signature == parent.structural_signature
                    for sibling in index.children.get(parent.parent_token, [])
                )
                >= self.options.minimum_cluster_size
            )
            confidence = 0.30
            confidence += 0.18 if page_has_job_context else 0.0
            confidence += 0.18 if local_job_context else 0.0
            confidence += 0.12 if parent_repeated else 0.0
            confidence += 0.12 if 4 <= len(text) <= 200 else 0.0
            confidence += 0.08 if node.role in {"link", "heading"} else 0.0
            confidence = min(1.0, confidence)
            if confidence < max(self.options.minimum_confidence, 0.70):
                continue
            evidence = {
                "origin": "adaptive_dom_individual_link",
                "contract_version": DOM_DISCOVERY_CONTRACT_VERSION,
                "frame_id": index.frame.frame_id,
                "frame_url": index.frame.frame_url,
                "node_token": node.node_token,
                "local_job_context": local_job_context,
                "parent_repeated": parent_repeated,
                # An isolated link is useful input to the normal URL ranker, but
                # page-level careers wording is not enough evidence to override
                # a rejection. Only repeated, structurally grounded records can
                # preserve an unfamiliar URL shape.
                "evidence_preserving": False,
            }
            candidates.append(
                DiscoveryCandidate.from_url(
                    node.href,
                    confidence=confidence,
                    evidence=evidence,
                ).model_copy(update={"title_hint": text})
            )
        return candidates

    @staticmethod
    def _cluster_confidence(
        *,
        cluster_size: int,
        unique_identity_ratio: float,
        median_fields: float,
        table_rows: bool,
        page_has_job_context: bool,
        row_has_job_evidence: bool,
    ) -> float:
        score = 0.18
        score += min(0.20, cluster_size * 0.02)
        score += min(0.20, unique_identity_ratio * 0.20)
        score += min(0.14, median_fields * 0.035)
        score += 0.14 if table_rows else (0.07 if row_has_job_evidence else 0.05)
        score += 0.08 if page_has_job_context else 0.0
        score += 0.06 if row_has_job_evidence else 0.0
        return round(min(score, 1.0), 4)

    @staticmethod
    def _select_primary_url(
        nodes: list[DomNodeEvidence],
        urls: list[str],
    ) -> str | None:
        if not urls:
            return None
        scored: list[tuple[float, int, str]] = []
        for position, node in enumerate(nodes):
            if not node.href:
                continue
            text = " ".join(str(node.text or "").split())
            score = 1.0
            score += 1.0 if node.role == "heading" else 0.0
            score += 0.8 if 8 <= len(text) <= 200 else 0.0
            score += min(len(text), 120) / 200.0
            score -= 1.5 if _DATE_TEXT.fullmatch(text) else 0.0
            score -= 2.0 if _NAVIGATION_TEXT.fullmatch(text) else 0.0
            score -= 0.8 if len(text) <= 3 else 0.0
            scored.append((score, -position, node.href))
        return max(scored)[2] if scored else urls[0]

    @staticmethod
    def _select_title(
        nodes: list[DomNodeEvidence],
        primary_url: str | None,
    ) -> str | None:
        scored: list[tuple[float, int, str]] = []
        for position, node in enumerate(nodes):
            text = " ".join(str(node.text or "").split())
            if not text or _NAVIGATION_TEXT.fullmatch(text):
                continue
            score = 0.0
            score += 3.0 if node.role == "heading" else 0.0
            score += 2.0 if primary_url and node.href == primary_url else 0.0
            score += 1.0 if 8 <= len(text) <= 200 else 0.0
            score += min(len(text), 120) / 120.0
            score -= 4.0 if _DATE_TEXT.fullmatch(text) else 0.0
            score -= 1.5 if len(text) <= 3 else 0.0
            scored.append((score, -position, text[:1_000]))
        return max(scored)[2] if scored else None

    @staticmethod
    def _select_location(
        text_values: tuple[str, ...],
        title_hint: str | None,
    ) -> str | None:
        for value in text_values:
            if value == title_hint or _DATE_TEXT.fullmatch(value):
                continue
            if 2 <= len(value) <= 120 and not _NAVIGATION_TEXT.fullmatch(value):
                return value
        return None

    @staticmethod
    def _source_job_id(nodes: Iterable[DomNodeEvidence]) -> str | None:
        preferred = (
            "data-job-id",
            "data-requisition-id",
            "data-req-id",
            "data-position-id",
            "data-posting-id",
        )
        for node in nodes:
            for key in preferred:
                value = " ".join(str(node.attributes.get(key) or "").split())
                if value:
                    return value[:500]
        return None

    @staticmethod
    def _useful_text(value: str | None) -> str | None:
        text = " ".join(str(value or "").split())
        if not text or len(text) > 1_000:
            return None
        return text

    @staticmethod
    def _same_document_route(candidate_url: str, listing_url: str) -> bool:
        """Reject sorting/filter/pagination links that only mutate query state."""

        candidate = urlsplit(candidate_url)
        listing = urlsplit(listing_url)
        return (
            candidate.scheme.lower(),
            (candidate.hostname or "").lower(),
            candidate.path.rstrip("/") or "/",
        ) == (
            listing.scheme.lower(),
            (listing.hostname or "").lower(),
            listing.path.rstrip("/") or "/",
        )

    @staticmethod
    def _deduplicate(candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
        selected: dict[str, DiscoveryCandidate] = {}
        order: dict[str, int] = {}
        for position, candidate in enumerate(candidates):
            key = candidate.detail_url or candidate.identity_key
            existing = selected.get(key)
            if existing is None or candidate.confidence > existing.confidence:
                selected[key] = candidate
                order.setdefault(key, position)
        return sorted(
            selected.values(),
            key=lambda candidate: (-candidate.confidence, order[candidate.detail_url or candidate.identity_key]),
        )


def preserve_evidence_backed_urls(
    ranked_urls: list[str],
    candidates: Iterable[DiscoveryCandidate],
    *,
    minimum_confidence: float = 0.74,
    ranking_metrics: dict[str, object] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Retain only independently grounded URLs with unfamiliar shapes.

    This is deliberately narrower than candidate discovery.  DOM candidates
    still reach the ordinary URL ranker, but isolated navigation links can no
    longer override it merely because the page contains the word ``jobs``.
    Structured JSON records and observed linkless-card navigation may override
    a shape-only rejection, but never a ranker's hard safety/navigation reject.
    """

    selected = list(dict.fromkeys(str(url).strip() for url in ranked_urls if str(url).strip()))
    preserved = 0
    considered = 0
    rejected_hard = 0
    rejected_weak_evidence = 0
    hard_rejected_urls = {
        str(item.get("url") or "").strip()
        for item in list((ranking_metrics or {}).get("rejected_candidates") or [])
        if isinstance(item, dict) and bool(item.get("hard_reject"))
    }
    for candidate in candidates:
        if not candidate.detail_url:
            continue
        origin = str(candidate.evidence.get("origin") or "")
        if origin not in {
            "adaptive_dom_repeated_cluster",
            "adaptive_dom_linkless_interaction",
            "network_json_record",
            "inline_json_record",
        }:
            continue
        considered += 1
        if candidate.detail_url in hard_rejected_urls:
            rejected_hard += 1
            continue
        if candidate.confidence < minimum_confidence:
            rejected_weak_evidence += 1
            continue
        if not bool(candidate.evidence.get("evidence_preserving")):
            rejected_weak_evidence += 1
            continue
        if origin == "adaptive_dom_repeated_cluster" and not bool(
            candidate.evidence.get("structural_job_grounding")
        ):
            rejected_weak_evidence += 1
            continue
        if origin == "adaptive_dom_linkless_interaction" and not bool(
            candidate.evidence.get("source_structural_job_grounding")
        ):
            rejected_weak_evidence += 1
            continue
        if origin in {"network_json_record", "inline_json_record"} and not bool(
            candidate.evidence.get("structured_job_record")
        ):
            rejected_weak_evidence += 1
            continue
        if candidate.detail_url not in selected:
            selected.append(candidate.detail_url)
            preserved += 1
    return selected, {
        "adaptive_candidates_considered": considered,
        "adaptive_urls_preserved": preserved,
        "adaptive_urls_rejected_hard": rejected_hard,
        "adaptive_urls_rejected_weak_evidence": rejected_weak_evidence,
    }
