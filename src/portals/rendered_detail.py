from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..crawl.browser_evidence import (
    BrowserEvidenceCollector,
    BrowserEvidenceOptions,
    BrowserEvidenceReport,
)
from ..crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from ..schemas import BrowserSettings, JobPosting
from .job_evidence import (
    is_plausible_job_title,
    job_detail_signal_count,
    job_title_rejection_reason,
    plausible_location,
)
from .json_discovery import JsonCandidateDiscoverer


RENDERED_DETAIL_CONTRACT_VERSION = "1.1"
_DETAIL_SIGNAL = re.compile(
    r"\b(job\s+description|position\s+summary|about\s+(?:the\s+)?role|"
    r"responsibilit(?:y|ies)|duties|qualifications?|requirements?|"
    r"required\s+(?:skills?|experience)|preferred\s+(?:skills?|qualifications?)|"
    r"must[- ]haves?|"
    r"what\s+you(?:'|’)ll\s+do|skills?\s+(?:and|&)\s+experience|"
    r"benefits|compensation|employment\s+type|requisition|job\s+(?:id|reference)|"
    r"apply\s+(?:now|for))\b",
    re.I,
)
_GENERIC_TITLE = re.compile(
    r"^(jobs?|careers?|openings?|opportunities|search\s+(?:jobs?|results)|"
    r"job\s+search|employment|home|about(?:\s+us)?|details?|view\s+(?:job|role))$",
    re.I,
)
_CONTROL_TEXT = re.compile(
    r"^(apply|apply now|save|share|back|next|previous|close|cancel|"
    r"filter|sort|search|load more|view more|read more)$",
    re.I,
)
_LOCATION_LABEL = re.compile(
    r"\b(?:job\s+)?location\s*[:\-]\s*(?P<value>.+)$",
    re.I,
)
_LOCATION_SHAPE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}(?:\s+\d{5})?)\b"
)
_REFERENCE = re.compile(
    r"\b(?:job\s+(?:id|number|reference)|requisition(?:\s+(?:id|number))?|"
    r"req(?:uisition)?\s*(?:id|number|#|no\.)|reference\s*(?:id|number|#|no\.)?)\s*"
    r"[:#\-]?\s*(?P<value>(?=[A-Za-z0-9._/-]*\d)[A-Za-z0-9][A-Za-z0-9._/-]{2,80})\b",
    re.I,
)
_POSTED = re.compile(
    r"\b(?:posted|date\s+posted)\s*[:\-]?\s*"
    r"(?P<value>(?:today|yesterday|\d+\s+(?:hours?|days?|weeks?)\s+ago|"
    r"\d{4}-\d{1,2}-\d{1,2}|[A-Z][a-z]+\s+\d{1,2},\s+\d{4}))\b",
    re.I,
)
_EMPLOYMENT = re.compile(
    r"\b(full[- ]?time|part[- ]?time|contract(?:or)?|temporary|permanent|internship)\b",
    re.I,
)
_COMPENSATION = re.compile(
    r"(?:[$£€]\s?\d[\d,.]*(?:\s*[-–]\s*[$£€]?\s?\d[\d,.]*)?"
    r"(?:\s*(?:per|/)\s*(?:hour|year|annum))?)",
    re.I,
)
_EXCLUDED_TAGS = {"nav", "header", "footer", "form", "input", "select", "option"}
_EXCLUDED_ROLES = {"navigation", "banner", "contentinfo", "search", "combobox", "textbox"}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _text(value).lower()).strip()


def _node_fingerprint(node: DomNodeEvidence) -> tuple[Any, ...]:
    return (
        node.tag,
        node.role,
        _normalized(node.text),
        str(node.href or ""),
        tuple(sorted((str(key), str(value)) for key, value in node.attributes.items())),
        node.structural_signature,
    )


@dataclass(frozen=True)
class RenderedDetailOptions:
    minimum_summary_chars: int = 120
    minimum_modal_delta_chars: int = 80
    maximum_summary_chars: int = 12_000
    maximum_text_parts: int = 250

    def __post_init__(self) -> None:
        if not 40 <= self.minimum_summary_chars <= 2_000:
            raise ValueError("minimum_summary_chars must be between 40 and 2000")
        if not 20 <= self.minimum_modal_delta_chars <= self.minimum_summary_chars:
            raise ValueError(
                "minimum_modal_delta_chars must be between 20 and minimum_summary_chars"
            )
        if self.maximum_summary_chars < self.minimum_summary_chars:
            raise ValueError("maximum_summary_chars must be at least minimum_summary_chars")
        if not 20 <= self.maximum_text_parts <= 2_000:
            raise ValueError("maximum_text_parts must be between 20 and 2000")


@dataclass(frozen=True)
class RenderedDetailResult:
    job: JobPosting | None
    reason: str
    confidence: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FrameText:
    frame: FrameDomSnapshot
    nodes: tuple[DomNodeEvidence, ...]
    text_parts: tuple[str, ...]
    title: str | None
    title_grounded: bool
    detail_signals: int
    strong_detail_signals: int
    source_job_id: str | None
    score: float
    delta_chars: int


class RenderedDetailExtractor:
    """Extract a job from semantic rendered evidence without selectors or an LLM.

    JSON records are preferred. The DOM lane then scores public frames/regions
    using headings, candidate hints, job-detail labels, independent metadata,
    and text density. A listing title by itself is never sufficient.
    """

    def __init__(self, options: RenderedDetailOptions | None = None) -> None:
        self.options = options or RenderedDetailOptions()

    def extract(
        self,
        report: BrowserEvidenceReport,
        *,
        fallback_url: str,
        title_hint: str | None = None,
        location_hint: str | None = None,
        baseline_report: BrowserEvidenceReport | None = None,
    ) -> RenderedDetailResult:
        structured = self._structured_job(
            report,
            fallback_url=fallback_url,
            title_hint=title_hint,
        )
        if structured is not None and is_plausible_job_title(structured.title):
            return RenderedDetailResult(
                job=structured,
                reason="structured_job_record",
                confidence=0.99,
                metrics={
                    "contract_version": RENDERED_DETAIL_CONTRACT_VERSION,
                    "strategy": "structured_json",
                    "frames_considered": len(report.frames),
                },
            )

        baseline_counts = self._baseline_counts(baseline_report)
        frames = [
            value
            for frame in report.frames
            if (
                value := self._frame_text(
                    frame,
                    title_hint=title_hint,
                    baseline_counts=baseline_counts,
                    modal_mode=baseline_report is not None,
                )
            )
            is not None
        ]
        metrics: dict[str, Any] = {
            "contract_version": RENDERED_DETAIL_CONTRACT_VERSION,
            "strategy": "semantic_rendered_dom",
            "frames_considered": len(report.frames),
            "frames_qualified": len(frames),
            "modal_delta": baseline_report is not None,
        }
        if not frames:
            return RenderedDetailResult(
                job=None,
                reason="no_semantically_grounded_detail_region",
                metrics=metrics,
            )

        selected = max(frames, key=lambda value: value.score)
        summary = self._summary(selected.text_parts, selected.title)[
            : self.options.maximum_summary_chars
        ]
        minimum_chars = (
            self.options.minimum_modal_delta_chars
            if baseline_report is not None
            else self.options.minimum_summary_chars
        )
        metrics.update(
            {
                "selected_frame_id": selected.frame.frame_id,
                "detail_signals": selected.detail_signals,
                "strong_detail_signals": selected.strong_detail_signals,
                "summary_chars": len(summary),
                "delta_chars": selected.delta_chars,
                "title_grounded": selected.title_grounded,
            }
        )
        title_rejection = job_title_rejection_reason(selected.title)
        if title_rejection is not None:
            return RenderedDetailResult(
                job=None,
                reason=f"invalid_job_title:{title_rejection}",
                metrics=metrics,
            )
        if not selected.title_grounded:
            return RenderedDetailResult(
                job=None,
                reason="title_not_grounded_in_rendered_document",
                metrics=metrics,
            )
        if selected.detail_signals < 1:
            return RenderedDetailResult(
                job=None,
                reason="missing_job_detail_signals",
                metrics=metrics,
            )
        if selected.strong_detail_signals < 1:
            return RenderedDetailResult(
                job=None,
                reason="missing_strong_job_detail_signals",
                metrics=metrics,
            )
        if len(summary) < minimum_chars:
            return RenderedDetailResult(
                job=None,
                reason="insufficient_rendered_detail_text",
                metrics=metrics,
            )

        combined = " | ".join(selected.text_parts)
        location = plausible_location(location_hint) or self._location(selected.text_parts)
        reference = (
            selected.source_job_id
            or self._first_match(_REFERENCE, combined)
            or self._reference_from_url(fallback_url)
        )
        posted = self._first_match(_POSTED, combined)
        employment = self._first_text_match(_EMPLOYMENT, selected.text_parts)
        compensation = self._first_text_match(_COMPENSATION, selected.text_parts)
        apply_url = self._apply_url(selected.nodes) or fallback_url
        job = JobPosting(
            title=selected.title,
            job_url=fallback_url,
            apply_url=apply_url,
            location_text=location,
            employment_type=employment,
            compensation_text=compensation,
            posted_date=posted,
            summary=summary,
            job_reference=reference,
        )
        confidence = min(
            0.97,
            0.66
            + min(0.12, selected.detail_signals * 0.03)
            + (0.08 if location else 0.0)
            + (0.06 if reference else 0.0)
            + (0.05 if baseline_report is not None else 0.0),
        )
        return RenderedDetailResult(
            job=job,
            reason="semantic_rendered_detail",
            confidence=round(confidence, 4),
            metrics=metrics,
        )

    @staticmethod
    def _structured_job(
        report: BrowserEvidenceReport,
        *,
        fallback_url: str,
        title_hint: str | None,
    ) -> JobPosting | None:
        batch = JsonCandidateDiscoverer().discover(report)
        candidates = [
            candidate
            for candidate in batch.candidates
            if candidate.preextracted_job is not None
        ]
        if not candidates:
            return None
        normalized_hint = _normalized(title_hint)

        def score(candidate: Any) -> tuple[int, float]:
            job = candidate.preextracted_job
            normalized_title = _normalized(job.title if job is not None else "")
            title_match = int(
                bool(normalized_hint)
                and bool(normalized_title)
                and (
                    normalized_hint == normalized_title
                    or normalized_hint in normalized_title
                    or normalized_title in normalized_hint
                )
            )
            return title_match, float(candidate.confidence)

        selected = max(candidates, key=score)
        job = selected.preextracted_job
        assert job is not None
        return job.model_copy(update={"job_url": fallback_url})

    @staticmethod
    def _baseline_counts(
        report: BrowserEvidenceReport | None,
    ) -> Counter[tuple[Any, ...]] | None:
        if report is None:
            return None
        return Counter(
            _node_fingerprint(node)
            for frame in report.frames
            for node in frame.nodes
            if node.visible
        )

    def _frame_text(
        self,
        frame: FrameDomSnapshot,
        *,
        title_hint: str | None,
        baseline_counts: Counter[tuple[Any, ...]] | None,
        modal_mode: bool,
    ) -> _FrameText | None:
        by_token = {node.node_token: node for node in frame.nodes}
        remaining = Counter(baseline_counts or {})
        usable: list[DomNodeEvidence] = []
        delta_nodes: list[DomNodeEvidence] = []
        for node in frame.nodes:
            if not node.visible or self._excluded(node, by_token):
                continue
            usable.append(node)
            if baseline_counts is None:
                delta_nodes.append(node)
                continue
            fingerprint = _node_fingerprint(node)
            if remaining[fingerprint] > 0:
                remaining[fingerprint] -= 1
            else:
                delta_nodes.append(node)

        evidence_nodes = delta_nodes if modal_mode else usable
        parts = self._text_parts(evidence_nodes)
        if not parts:
            return None
        combined = " | ".join(parts)
        detail_signals = len({_normalized(match.group(0)) for match in _DETAIL_SIGNAL.finditer(combined)})
        strong_detail_signals = job_detail_signal_count(combined)
        title, title_grounded = self._select_title(
            usable,
            title_hint=title_hint,
            frame_title=frame.title,
        )
        source_job_id = self._source_job_id(usable, combined)
        delta_chars = len(" ".join(parts))
        score = float(detail_signals * 3 + strong_detail_signals * 3)
        score += min(8.0, delta_chars / 500.0)
        score += 6.0 if title_grounded else 0.0
        score += 3.0 if any(node.role == "dialog" for node in usable) else 0.0
        score += 2.0 if source_job_id else 0.0
        return _FrameText(
            frame=frame,
            nodes=tuple(usable),
            text_parts=tuple(parts),
            title=title,
            title_grounded=title_grounded,
            detail_signals=detail_signals,
            strong_detail_signals=strong_detail_signals,
            source_job_id=source_job_id,
            score=score,
            delta_chars=delta_chars,
        )

    @staticmethod
    def _excluded(
        node: DomNodeEvidence,
        by_token: dict[str, DomNodeEvidence],
    ) -> bool:
        current: DomNodeEvidence | None = node
        for _ in range(30):
            if current is None:
                break
            if current.tag in _EXCLUDED_TAGS or current.role in _EXCLUDED_ROLES:
                return True
            current = by_token.get(current.parent_token or "")
        return False

    def _text_parts(self, nodes: Iterable[DomNodeEvidence]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for node in nodes:
            value = _text(node.text)
            normalized = _normalized(value)
            if not value or not normalized or normalized in seen:
                continue
            if _CONTROL_TEXT.fullmatch(value) or len(value) < 3:
                continue
            seen.add(normalized)
            values.append(value[:2_000])
            if len(values) >= self.options.maximum_text_parts:
                break
        return values

    @staticmethod
    def _select_title(
        nodes: Iterable[DomNodeEvidence],
        *,
        title_hint: str | None,
        frame_title: str | None,
    ) -> tuple[str | None, bool]:
        values = list(nodes)
        normalized_hint = _normalized(title_hint)
        scored: list[tuple[float, int, str]] = []
        for position, node in enumerate(values):
            value = _text(node.text)
            normalized_value = _normalized(value)
            if not value or len(value) > 300 or not is_plausible_job_title(value):
                continue
            score = 0.0
            score += 5.0 if node.role == "heading" or re.fullmatch(r"h[1-3]", node.tag) else 0.0
            if normalized_hint and normalized_value:
                if normalized_hint == normalized_value:
                    score += 12.0
                elif normalized_hint in normalized_value or normalized_value in normalized_hint:
                    score += 7.0
            score += 1.0 if 5 <= len(value) <= 160 else 0.0
            score -= 4.0 if _DETAIL_SIGNAL.search(value) else 0.0
            scored.append((score, -position, value))
        if scored:
            selected = max(scored)[2]
            grounded = bool(
                any(_normalized(node.text) == _normalized(selected) for node in values)
            )
            return selected, grounded
        fallback = _text(title_hint) or _text(frame_title)
        if fallback and is_plausible_job_title(fallback):
            normalized_fallback = _normalized(fallback)
            grounded = any(
                normalized_fallback
                and normalized_fallback in _normalized(node.text)
                for node in values
            )
            return fallback[:1_000], grounded
        return None, False

    @staticmethod
    def _source_job_id(nodes: Iterable[DomNodeEvidence], combined: str) -> str | None:
        keys = (
            "data-job-id",
            "data-requisition-id",
            "data-req-id",
            "data-position-id",
            "data-posting-id",
        )
        for node in nodes:
            for key in keys:
                value = _text(node.attributes.get(key))
                if value:
                    return value[:500]
        match = _REFERENCE.search(combined)
        return _text(match.group("value"))[:500] if match else None

    @staticmethod
    def _summary(parts: Iterable[str], title: str | None) -> str:
        title_value = _normalized(title)
        selected: list[str] = []
        for part in parts:
            if _normalized(part) == title_value:
                continue
            selected.append(part)
        return "\n".join(selected)

    @staticmethod
    def _location(parts: Iterable[str]) -> str | None:
        values = list(parts)
        for value in values:
            match = _LOCATION_LABEL.search(value)
            if match:
                if location := plausible_location(match.group("value")):
                    return location
        for value in values:
            if location := plausible_location(value):
                return location
        return None

    @staticmethod
    def _reference_from_url(value: str) -> str | None:
        try:
            from urllib.parse import unquote, urlsplit

            parsed = urlsplit(value)
        except ValueError:
            return None
        logical = "/".join(
            part.strip("!#/")
            for part in (unquote(parsed.path), unquote(parsed.fragment))
            if part.strip("!#/")
        )
        segments = [segment for segment in logical.split("/") if segment]
        for segment in reversed(segments):
            if re.fullmatch(r"\d{4,}", segment):
                return segment
            match = re.search(r"(?:^|[-_])([A-Za-z]{0,6}-?\d{4,})(?:$|[-_])", segment)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _first_match(pattern: re.Pattern[str], value: str) -> str | None:
        match = pattern.search(value)
        if not match:
            return None
        return _text(match.groupdict().get("value") or match.group(0))[:1_000]

    @staticmethod
    def _first_text_match(
        pattern: re.Pattern[str],
        parts: Iterable[str],
    ) -> str | None:
        for value in parts:
            match = pattern.search(value)
            if match:
                return _text(match.group(0))[:1_000]
        return None

    @staticmethod
    def _apply_url(nodes: Iterable[DomNodeEvidence]) -> str | None:
        for node in nodes:
            if node.href and re.search(r"\bapply\b", _text(node.text), re.I):
                return node.href
        return None


class RenderedDetailExtractionService:
    """Capture one public detail page and run deterministic semantic extraction."""

    def __init__(
        self,
        browser_settings: BrowserSettings,
        *,
        evidence_options: BrowserEvidenceOptions | None = None,
        extraction_options: RenderedDetailOptions | None = None,
        collector: Any | None = None,
    ) -> None:
        self.browser_settings = browser_settings
        self.evidence_options = evidence_options or BrowserEvidenceOptions()
        self.extractor = RenderedDetailExtractor(extraction_options)
        self.collector = collector

    async def extract(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
        title_hint: str | None = None,
        location_hint: str | None = None,
    ) -> RenderedDetailResult:
        collector = self.collector or BrowserEvidenceCollector(
            self.browser_settings,
            options=self.evidence_options,
        )
        report = await collector.capture(url, allowed_hosts=tuple(allowed_hosts))
        return self.extractor.extract(
            report,
            fallback_url=report.final_url or url,
            title_hint=title_hint,
            location_hint=location_hint,
        )
