from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any


_JOB_SIGNALS = (
    "job description",
    "position description",
    "responsibilities",
    "qualifications",
    "required skills",
    "preferred qualifications",
    "apply now",
    "employment type",
    "requisition id",
    "job id",
    "job#",
    "salary range",
    "date posted",
    "dateposted",
    "hiringorganization",
)

_STRONG_ACCESS_MARKERS = (
    "access denied",
    "verify you are human",
    "checking your browser before accessing",
    "attention required! | cloudflare",
    "unusual traffic from your computer network",
    "robot check",
    "enable javascript and cookies to continue",
)

_CHALLENGE_MARKERS = (
    "/cdn-cgi/challenge-platform/",
    "cf-chl-",
    "cloudflare ray id",
    "challenges.cloudflare.com/turnstile",
    "hcaptcha.com/1/api.js",
    "recaptcha/api2/bframe",
)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "svg", "template"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "svg", "template"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def visible_text(value: Any, *, limit: int = 2_000_000) -> str:
    raw = str(value or "")[: max(1, int(limit))]
    parser = _VisibleTextParser()
    try:
        parser.feed(raw)
        rendered = " ".join(parser.parts)
    except Exception:
        rendered = re.sub(r"<[^>]+>", " ", raw)
    return " ".join(rendered.split())


def job_evidence_count(value: Any) -> int:
    lowered = str(value or "").lower()
    return sum(1 for signal in _JOB_SIGNALS if signal in lowered)


def contains_jobposting_schema(value: Any) -> bool:
    lowered = str(value or "").lower()
    return "application/ld+json" in lowered and "jobposting" in lowered


@dataclass(frozen=True)
class PageQualityAssessment:
    blocked: bool
    reason: str | None
    visible_characters: int
    job_evidence: int
    has_jobposting_schema: bool
    challenge_markers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["challenge_markers"] = list(self.challenge_markers)
        return payload


def assess_page_quality(
    *,
    html: Any,
    text_content: Any = None,
) -> PageQualityAssessment:
    """Classify access-control surfaces without treating footer CAPTCHA text as a block."""
    raw = str(html or "")[:2_000_000]
    supplemental = str(text_content or "")[:500_000]
    combined = f"{raw}\n{supplemental}"
    lowered = combined.lower()
    rendered = visible_text(f"{raw}\n{supplemental}")
    evidence = job_evidence_count(combined)
    schema = contains_jobposting_schema(combined)
    challenge_markers = tuple(marker for marker in _CHALLENGE_MARKERS if marker in lowered)

    # A real Schema.org job document is stronger evidence than generic words
    # such as CAPTCHA or "access denied" appearing in a footer or job body.
    if schema:
        return PageQualityAssessment(
            blocked=False,
            reason=None,
            visible_characters=len(rendered),
            job_evidence=evidence,
            has_jobposting_schema=True,
            challenge_markers=challenge_markers,
        )

    strong = next((marker for marker in _STRONG_ACCESS_MARKERS if marker in lowered), None)
    if strong and evidence < 2:
        return PageQualityAssessment(
            blocked=True,
            reason=f"access-control marker: {strong}",
            visible_characters=len(rendered),
            job_evidence=evidence,
            has_jobposting_schema=False,
            challenge_markers=challenge_markers,
        )

    # Cloudflare/reCAPTCHA scripts are common on legitimate pages. They prove
    # blocking only when the useful document body was suppressed as well.
    sparse = len(rendered) < 300
    if challenge_markers and sparse and evidence < 2:
        return PageQualityAssessment(
            blocked=True,
            reason="challenge markup accompanied by a sparse rendered document",
            visible_characters=len(rendered),
            job_evidence=evidence,
            has_jobposting_schema=False,
            challenge_markers=challenge_markers,
        )

    generic_captcha = bool(re.search(r"\b(?:captcha|recaptcha|hcaptcha)\b", lowered))
    if generic_captcha and len(rendered) < 150 and evidence == 0:
        return PageQualityAssessment(
            blocked=True,
            reason="CAPTCHA is the only meaningful rendered content",
            visible_characters=len(rendered),
            job_evidence=evidence,
            has_jobposting_schema=False,
            challenge_markers=challenge_markers,
        )

    return PageQualityAssessment(
        blocked=False,
        reason=None,
        visible_characters=len(rendered),
        job_evidence=evidence,
        has_jobposting_schema=False,
        challenge_markers=challenge_markers,
    )


def assess_crawl_result(result: Any) -> PageQualityAssessment:
    raw_html = getattr(result, "html", None)
    primary = None
    for name in ("cleaned_html", "markdown", "fit_markdown", "text"):
        value = getattr(result, name, None)
        if value:
            primary = value
            break
    return assess_page_quality(html=raw_html or primary, text_content=primary)
