from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import unquote, urlsplit


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
_COMPANY_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "group",
    "inc",
    "llc",
    "ltd",
    "solutions",
    "staffing",
    "talent",
    "us",
    "usa",
}
_HOST_NOISE = {
    "apply",
    "career",
    "careers",
    "hire",
    "hiring",
    "job",
    "jobs",
    "recruiting",
    "www",
}
_URL_TITLE_NOISE = {
    "apply",
    "career",
    "careers",
    "detail",
    "details",
    "job",
    "jobs",
    "job-posting",
    "opening",
    "openings",
    "position",
    "positions",
    "role",
    "search",
}

_GENERIC_TITLES = {
    "about",
    "about us",
    "apply",
    "apply now",
    "careers",
    "contact us",
    "details",
    "employment",
    "find a job",
    "find jobs",
    "find work",
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
    "browse jobs",
    "browse openings",
    "search jobs",
    "search results",
    "view job",
    "view openings",
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


def _host_brand_key(url: object) -> str:
    try:
        hostname = str(urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    labels = [label for label in hostname.split(".") if label]
    if len(labels) < 2:
        return ""
    meaningful = [label for label in labels[:-1] if label not in _HOST_NOISE]
    return re.sub(r"[^a-z0-9]+", "", meaningful[-1] if meaningful else "")


def _title_matches_host_brand(title: object, job_url: object) -> bool:
    brand = _host_brand_key(job_url)
    title_words = normalized_evidence_key(title).split()
    compact_title = "".join(title_words)
    if len(brand) < 4 or len(compact_title) < 4:
        return False
    if compact_title == brand or brand in compact_title:
        return True
    if brand.startswith(compact_title) and brand[len(compact_title) :] in {"us", "usa"}:
        return True
    if compact_title.startswith(brand):
        remainder = normalized_evidence_key(title)[len(normalized_evidence_key(title).split()[0]) :]
        remainder_words = set(normalized_evidence_key(remainder).split())
        return bool(remainder_words) and remainder_words <= _COMPANY_SUFFIXES
    return False


def job_title_source_rejection_reason(
    value: object,
    *,
    job_url: object,
    job_reference: object = None,
    summary: object = None,
) -> str | None:
    """Reject a site/organization title masquerading as a job title.

    The decision uses generic source evidence: hostname overlap, a website-level
    JSON-LD identifier, and the absence of job-detail language.  It does not
    contain portal names or selectors.
    """

    rejection = job_title_rejection_reason(value)
    if rejection is not None:
        return rejection
    if not _title_matches_host_brand(value, job_url):
        return None
    title_words = set(normalized_evidence_key(value).split())
    reference = normalize_evidence_text(job_reference).lower()
    website_reference = bool(
        reference
        and (
            reference.endswith("#website")
            or normalized_evidence_key(reference).endswith(" website")
        )
    )
    if "jobs" in title_words or "careers" in title_words:
        return "site_brand_title"
    if website_reference and job_detail_signal_count(normalize_evidence_text(summary)) == 0:
        return "website_schema_title"
    return None


def infer_job_title_from_url(value: object) -> str | None:
    """Infer a conservative title hint from an unfamiliar detail URL slug.

    This is a hint for rendered-DOM scoring, never sufficient evidence by
    itself. Numeric requisition suffixes and navigation segments are removed.
    """

    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return None
    segments = [unquote(segment).strip() for segment in parsed.path.split("/") if segment.strip()]
    for raw in reversed(segments):
        lowered = raw.lower().strip("-_")
        if not lowered or lowered in _URL_TITLE_NOISE or re.fullmatch(r"\d{3,}", lowered):
            continue
        cleaned = re.sub(r"(?:[-_](?:[A-Z]{1,8}[-_]?)?\d{4,})+$", "", raw)
        cleaned = re.sub(r"[-_]+", " ", cleaned)
        cleaned = normalize_evidence_text(cleaned).strip(" -|:")
        words = normalized_evidence_key(cleaned).split()
        if not 2 <= len(words) <= 16 or not any(len(word) >= 3 for word in words):
            continue
        if job_title_rejection_reason(cleaned) is not None:
            continue
        return cleaned[:300]
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
