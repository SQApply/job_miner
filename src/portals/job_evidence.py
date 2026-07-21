from __future__ import annotations

import re
from typing import Iterable


_PURE_DATE = re.compile(
    r"^(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|"
    r"[A-Z][a-z]+\s+\d{1,2},\s+\d{4}|"
    r"\d+\s+(?:minute|hour|day|week|month)s?\s+ago)$",
    re.I,
)
_DATED_LABEL = re.compile(
    r"^(?:added|posted|updated|published|date\s+posted)\s*[-:|]?\s*"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|"
    r"[A-Z][a-z]+\s+\d{1,2},\s+\d{4}|today|yesterday|"
    r"\d+\s+(?:minute|hour|day|week|month)s?\s+ago)$",
    re.I,
)
_LOCATION_LABEL = re.compile(r"^(?:job\s+|work\s+)?location\s*[:\-]\s*(.+)$", re.I)
_LOCATION_SHAPE = re.compile(
    r"^(?:(?:remote|hybrid|onsite|on-site)(?:\s*[-,(].{1,80})?|"
    r"[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}(?:\s+\d{5})?|"
    r"[A-Z][A-Za-z .'-]+,\s*[A-Z][A-Za-z .'-]+|"
    r"City\s+\d+)$",
    re.I,
)
_JOB_DETAIL_SIGNAL = re.compile(
    r"\b(job\s+(?:description|summary)|position\s+(?:description|summary)|"
    r"about\s+(?:the\s+)?role|must[- ]haves?|"
    r"responsibilit(?:y|ies)|duties|qualifications?|requirements?|"
    r"required\s+(?:skills?|experience)|preferred\s+(?:skills?|qualifications?)|"
    r"what\s+you(?:'|’)ll\s+do|skills?\s+(?:and|&)\s+experience|"
    r"work\s+location|employment\s+type|requisition(?:\s+(?:id|number))?|"
    r"job\s+(?:id|reference)|apply\s+(?:now|for))\b",
    re.I,
)
_MARKETING_PHRASE = re.compile(
    r"\b(connects?\s+you|help(?:s|ing)?\s+(?:you|employers|companies)|"
    r"focused\s+expertise\s+across|build\s+(?:your|great)\s+team|"
    r"workforce\s+solutions?|find\s+talent|hire\s+talent|"
    r"learn\s+more|explore\s+our\s+services)\b",
    re.I,
)
_URLISH = re.compile(r"^(?:https?://|www\.)|\.(?:com|net|org)(?:/|$)", re.I)
_RELATED_JOBS_HEADING = re.compile(r"^related\s+.{2,180}\s+jobs?$", re.I)

_GENERIC_TITLES = {
    "about",
    "about us",
    "apply",
    "apply now",
    "careers",
    "contact us",
    "details",
    "employment",
    "home",
    "internal careers",
    "job",
    "job details",
    "job search",
    "job seekers",
    "jobs",
    "learn more",
    "open positions",
    "openings",
    "opportunities",
    "permanent recruitment",
    "resources",
    "resume upload",
    "search jobs",
    "search results",
    "view job",
    "view role",
    "website",
}
_SECTION_TITLES = {
    "about the job",
    "about the position",
    "about the role",
    "benefits",
    "compensation",
    "description",
    "duties",
    "employment type",
    "experience",
    "job description",
    "job overview",
    "job summary",
    "key responsibilities",
    "location",
    "must haves",
    "must have",
    "nice to haves",
    "overview",
    "position description",
    "position summary",
    "preferred qualifications",
    "preferred skills",
    "qualifications",
    "required experience",
    "required skills",
    "requirements",
    "responsibilities",
    "salary",
    "skills",
    "summary",
    "what you will do",
    "what you ll do",
}


def normalize_evidence_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def normalized_evidence_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_evidence_text(value).lower()).strip()


def job_title_rejection_reason(value: object) -> str | None:
    """Return a stable reason when rendered text is not a credible role title."""

    title = normalize_evidence_text(value).strip(" -|:")
    if len(title) < 3:
        return "missing_or_short_title"
    if len(title) > 300:
        return "overlong_title"
    key = normalized_evidence_key(title)
    if key in _GENERIC_TITLES:
        return "generic_navigation_title"
    if key in _SECTION_TITLES:
        return "section_heading_title"
    if _PURE_DATE.fullmatch(title) or _DATED_LABEL.fullmatch(title):
        return "date_label_title"
    if _RELATED_JOBS_HEADING.fullmatch(title):
        return "related_jobs_heading"
    if _URLISH.search(title):
        return "url_or_website_title"
    if _MARKETING_PHRASE.search(title):
        return "marketing_title"
    words = key.split()
    if len(words) >= 12 and any(
        token in words for token in ("you", "your", "our", "across", "employers")
    ):
        return "sentence_like_marketing_title"
    return None


def job_title_context_rejection_reason(
    value: object,
    context: str | Iterable[object],
) -> str | None:
    """Reject a plausible-looking label when the surrounding page proves it is taxonomy.

    Category names such as ``Accounting / Finance`` can look like role titles in
    isolation.  Many job boards disambiguate them with an exact, rendered
    ``Related <category> Jobs`` label.  Using that relationship is structural
    evidence, not a portal selector or a hard-coded category vocabulary.
    """

    rejection = job_title_rejection_reason(value)
    if rejection is not None:
        return rejection

    title_key = normalized_evidence_key(value)
    title_words = title_key.split()
    if not title_key or len(title_words) > 14:
        return None
    values = [context] if isinstance(context, str) else list(context)
    marker = f"related {title_key} jobs"
    singular_marker = f"related {title_key} job"
    for item in values:
        context_key = normalized_evidence_key(item)
        if marker in context_key or singular_marker in context_key:
            return "related_jobs_taxonomy_title"
    return None


def is_plausible_job_title(value: object) -> bool:
    return job_title_rejection_reason(value) is None


def plausible_location(value: object) -> str | None:
    text = normalize_evidence_text(value)
    if not text or len(text) > 160 or _PURE_DATE.fullmatch(text) or _DATED_LABEL.fullmatch(text):
        return None
    label = _LOCATION_LABEL.fullmatch(text)
    candidate = normalize_evidence_text(label.group(1) if label else text)
    if not candidate or normalized_evidence_key(candidate) in _SECTION_TITLES:
        return None
    return candidate[:1_000] if _LOCATION_SHAPE.fullmatch(candidate) else None


def job_detail_signal_count(values: str | Iterable[object]) -> int:
    if isinstance(values, str):
        text = values
    else:
        text = " | ".join(normalize_evidence_text(value) for value in values)
    return len(
        {
            normalized_evidence_key(match.group(0))
            for match in _JOB_DETAIL_SIGNAL.finditer(text)
        }
    )


def has_marketing_page_language(value: object) -> bool:
    return bool(_MARKETING_PHRASE.search(normalize_evidence_text(value)))
