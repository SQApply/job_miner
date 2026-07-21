from __future__ import annotations

import html as html_module
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from ..schemas import JobPosting
from ..warehouse.url_utils import canonical_job_url
from .job_evidence import (
    has_marketing_page_language,
    job_detail_signal_count,
    job_title_context_rejection_reason,
    job_title_source_rejection_reason,
    normalize_evidence_text,
)
from .page_quality import assess_crawl_result, visible_text


_ASSET_SUFFIXES = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}

_HARD_NEGATIVE_TOKENS = {
    "accessibility",
    "blog",
    "contact",
    "cookies",
    "events",
    "faq",
    "legal",
    "login",
    "news",
    "privacy",
    "resources",
    "signin",
    "signup",
    "sitemap",
    "terms",
}

_SOFT_NEGATIVE_TOKENS = {
    "about",
    "benefits",
    "candidate",
    "candidates",
    "community",
    "culture",
    "employers",
    "locations",
    "recruiters",
    "search",
    "talent",
}

_JOB_TOKENS = {
    "career",
    "careers",
    "job",
    "jobs",
    "opening",
    "openings",
    "opportunities",
    "opportunity",
    "position",
    "positions",
    "posting",
    "postings",
    "requisition",
    "requisitions",
    "role",
    "roles",
    "vacancies",
    "vacancy",
}

_DETAIL_TOKENS = {"apply", "detail", "details", "description", "view"}
_JOB_QUERY_KEYS = {
    "gh_jid",
    "job",
    "job_id",
    "jobid",
    "jobnumber",
    "jid",
    "posting_id",
    "postingid",
    "req",
    "reqid",
    "requisition",
    "requisitionid",
}
_TRACKING_ONLY_FRAGMENT_PREFIXES = ("utm_", "ga_")

_TELEMETRY_OR_CHALLENGE_HOSTS = (
    "doubleclick.net",
    "google-analytics.com",
    "google.com",
    "googletagmanager.com",
    "hcaptcha.com",
    "newrelic.com",
    "recaptcha.net",
)

_GENERIC_TITLES = {
    "about us",
    "careers",
    "contact us",
    "internal careers",
    "job search",
    "job seekers",
    "jobs",
    "open positions",
    "opportunities",
    "resources",
    "resume upload",
    "search jobs",
}

_CONTENT_JOB_SIGNALS = (
    "application/ld+json",
    "jobposting",
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
    "salary range",
    "compensation",
    "dateposted",
    "hiringorganization",
)

_NON_JOB_PAGE_SIGNALS = (
    "page not found",
    "404 not found",
    "access denied",
    "verify you are human",
    "captcha",
)

_PLACEHOLDER_JOB_SIGNALS = (
    "abc corporation",
    "example corporation",
    "example company",
    "lorem ipsum",
    "1234567890",
)


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", unquote(str(value or "")).lower())
        if token
    }


def _meaningful_hash_route(fragment: str) -> bool:
    normalized = unquote(str(fragment or "")).lower()
    if not normalized or normalized.startswith(_TRACKING_ONLY_FRAGMENT_PREFIXES):
        return False
    tokens = _tokens(normalized)
    return bool(tokens & (_JOB_TOKENS | _DETAIL_TOKENS))


def _logical_route_path(path: str, fragment: str) -> str:
    """Project a job-bearing SPA fragment into the route used for scoring."""

    physical = unquote(str(path or "/")).lower()
    raw_fragment = unquote(str(fragment or "")).strip()
    if not _meaningful_hash_route(raw_fragment):
        return physical
    fragment_route = raw_fragment.lstrip("!#/")
    if not fragment_route:
        return physical
    prefix = physical.rstrip("/")
    return re.sub(r"/{2,}", "/", f"{prefix}/{fragment_route}" or "/")


def _same_site(first: str, second: str) -> bool:
    first_parts = str(first or "").lower().rstrip(".").split(".")
    second_parts = str(second or "").lower().rstrip(".").split(".")
    return len(first_parts) >= 2 and len(second_parts) >= 2 and first_parts[-2:] == second_parts[-2:]


def _telemetry_or_challenge_host(hostname: str) -> bool:
    host = str(hostname or "").lower().rstrip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _TELEMETRY_OR_CHALLENGE_HOSTS)


def canonicalize_candidate_url(value: str) -> str:
    """Canonicalize a candidate while preserving job-bearing SPA hash routes."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        original = urlsplit(raw)
    except ValueError:
        return raw.rstrip("/")

    canonical = canonical_job_url(raw)
    if not canonical:
        return ""
    if not _meaningful_hash_route(original.fragment):
        return canonical

    parts = urlsplit(canonical)
    fragment = original.fragment.strip().rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, fragment))


@dataclass(frozen=True)
class CandidateAssessment:
    url: str
    score: int
    confidence: str
    hard_reject: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def assess_job_candidate_url(
    value: str,
    *,
    listing_url: str | None = None,
    platform_hint: str | None = None,
) -> CandidateAssessment:
    url = canonicalize_candidate_url(value)
    reasons: list[str] = []
    if not url:
        return CandidateAssessment(url="", score=-100, confidence="rejected", hard_reject=True, reasons=("empty",))

    try:
        parsed = urlsplit(url)
    except ValueError:
        return CandidateAssessment(url=url, score=-100, confidence="rejected", hard_reject=True, reasons=("malformed",))

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return CandidateAssessment(
            url=url,
            score=-100,
            confidence="rejected",
            hard_reject=True,
            reasons=("unsupported_scheme_or_host",),
        )
    if _telemetry_or_challenge_host(parsed.hostname):
        return CandidateAssessment(
            url=url,
            score=-100,
            confidence="rejected",
            hard_reject=True,
            reasons=("telemetry_or_challenge_host",),
        )

    physical_path = unquote(parsed.path or "/").lower()
    path = _logical_route_path(parsed.path, parsed.fragment)
    route_value = path
    tokens = _tokens(route_value)
    suffix = next(
        (suffix for suffix in _ASSET_SUFFIXES if physical_path.endswith(suffix)),
        None,
    )
    if suffix:
        return CandidateAssessment(
            url=url,
            score=-100,
            confidence="rejected",
            hard_reject=True,
            reasons=(f"asset:{suffix}",),
        )

    if listing_url and url == canonicalize_candidate_url(listing_url):
        return CandidateAssessment(
            url=url,
            score=-100,
            confidence="rejected",
            hard_reject=True,
            reasons=("same_as_listing",),
        )

    job_board_detail = bool(
        re.search(r"/(?:jb|job-board)/[^/]+/\d+(?:/|$)", path)
    )
    requisition_slug_detail = bool(
        re.search(r"/jobs?/[^/?#]+-\d{4,}(?:/|$)", path)
    )
    strong_path_signature = bool(
        re.search(r"/jobs?/details?(?:/|$)", path)
        or re.search(r"/jobs?/\d+(?:/|$)", path)
        or re.search(r"/(?:job|position|requisition|posting)/[^/]+", path)
        or job_board_detail
        or requisition_slug_detail
    )
    if (
        str(platform_hint or "").strip().lower() == "icims"
        and (
            str(parsed.hostname).lower() == "icims.com"
            or str(parsed.hostname).lower().endswith(".icims.com")
        )
        and not re.search(r"(?:^|/)jobs/\d+(?:/|$)", path)
    ):
        return CandidateAssessment(
            url=url,
            score=-40,
            confidence="rejected",
            hard_reject=True,
            reasons=("icims_listing_not_detail",),
        )
    if listing_url:
        try:
            listing_host = str(urlsplit(listing_url).hostname or "").lower()
        except ValueError:
            listing_host = ""
        if listing_host and not _same_site(parsed.hostname, listing_host) and not strong_path_signature:
            return CandidateAssessment(
                url=url,
                score=-50,
                confidence="rejected",
                hard_reject=True,
                reasons=("cross_site_without_job_signature",),
            )
    hard_negative = sorted(tokens & _HARD_NEGATIVE_TOKENS)
    if hard_negative and not strong_path_signature:
        return CandidateAssessment(
            url=url,
            score=-50,
            confidence="rejected",
            hard_reject=True,
            reasons=tuple(f"navigation:{token}" for token in hard_negative),
        )

    score = 0
    platform = str(platform_hint or "").strip().lower()

    if re.search(r"/jobs?/details?(?:/|$)", path):
        score += 12
        reasons.append("detail_path")
    elif re.search(r"/jobs?/\d+(?:/|$)", path):
        score += 10
        reasons.append("numeric_job_path")
    elif job_board_detail:
        score += 10
        reasons.append("job_board_detail_path")
    elif requisition_slug_detail:
        score += 12
        reasons.append("requisition_slug_detail_path")
    elif re.search(r"/(?:job|position|requisition|posting)/[^/]+", path):
        score += 8
        reasons.append("job_entity_path")

    if platform == "icims" and re.search(r"/jobs/\d+(?:/|$)", path):
        score += 6
        reasons.append("icims_detail_signature")

    positive_tokens = sorted(tokens & _JOB_TOKENS)
    if positive_tokens:
        score += 3
        reasons.append(f"job_tokens:{','.join(positive_tokens[:4])}")

    detail_tokens = sorted(tokens & _DETAIL_TOKENS)
    if detail_tokens:
        score += 2
        reasons.append(f"detail_tokens:{','.join(detail_tokens[:3])}")

    path_segments = [segment for segment in path.split("/") if segment]
    if any(re.fullmatch(r"\d{4,}", segment) for segment in path_segments):
        score += 3
        reasons.append("numeric_identifier")
    elif any(
        re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", segment)
        for segment in path_segments
    ):
        score += 3
        reasons.append("uuid_identifier")
    elif any(re.fullmatch(r"[a-z]{1,5}-?\d{4,}", segment) for segment in path_segments):
        score += 3
        reasons.append("requisition_identifier")

    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    matched_query_keys = sorted(query_keys & _JOB_QUERY_KEYS)
    if matched_query_keys:
        score += 8
        reasons.append(f"job_query:{','.join(matched_query_keys)}")

    soft_negative = sorted(tokens & _SOFT_NEGATIVE_TOKENS)
    if soft_negative:
        score -= 6
        reasons.append(f"navigation_tokens:{','.join(soft_negative[:4])}")

    if path.rstrip("/") in {"", "/job", "/jobs", "/career", "/careers", "/search", "/jobs/search"}:
        score -= 8
        reasons.append("listing_like_path")

    if score >= 8:
        confidence = "high"
    elif score >= 3:
        confidence = "medium"
    elif score >= 0:
        confidence = "low"
    else:
        confidence = "unlikely"
    return CandidateAssessment(
        url=url,
        score=score,
        confidence=confidence,
        hard_reject=False,
        reasons=tuple(reasons or ["no_detail_signal"]),
    )


def rank_job_candidate_urls(
    urls: Iterable[str],
    *,
    listing_url: str,
    platform_hint: str | None = None,
    preserve_low_confidence: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """Rank and conservatively filter browser-discovered URLs for bounded certification.

    If at least one high-confidence detail URL exists, only medium/high-confidence
    candidates are retained. When no detail signature exists, fail discovery rather
    than sending marketing/navigation pages to the browser and local GPU. The legacy
    fallback can be explicitly enabled for diagnostics, but certification never uses it.
    """
    raw = [str(value or "").strip() for value in urls if str(value or "").strip()]
    assessments: list[CandidateAssessment] = []
    seen: set[str] = set()
    duplicates = 0
    for value in raw:
        assessment = assess_job_candidate_url(
            value,
            listing_url=listing_url,
            platform_hint=platform_hint,
        )
        if not assessment.url:
            continue
        if assessment.url in seen:
            duplicates += 1
            continue
        seen.add(assessment.url)
        assessments.append(assessment)

    viable = [item for item in assessments if not item.hard_reject]
    confident = [item for item in viable if item.score >= 8]
    strict_platform = str(platform_hint or "").strip().lower() in {"icims"}
    fallback_preserved = not bool(confident) and bool(preserve_low_confidence) and not strict_platform
    if confident:
        selected = [item for item in viable if item.score >= 3]
    elif strict_platform or not preserve_low_confidence:
        # A medium score can come from phrases such as "job alerts" or a
        # listing root. Without at least one high-confidence detail signature,
        # sending these fallbacks to the browser/LLM only burns time and GPU and
        # can create false jobs. Fail discovery loudly instead.
        selected = []
    else:
        selected = viable
    selected.sort(key=lambda item: (-item.score, item.url))

    rejected = [item for item in assessments if item not in selected]
    metrics = {
        "strategy": "evidence_bound_url_ranking_v2",
        "input_urls": len(raw),
        "canonical_urls": len(assessments),
        "duplicate_urls": duplicates,
        "high_confidence_urls": len(confident),
        "selected_urls": len(selected),
        "rejected_urls": len(rejected),
        "fallback_preserved": fallback_preserved,
        "low_confidence_fallback_enabled": bool(preserve_low_confidence),
        "strict_platform_filter": strict_platform,
        "top_candidates": [item.to_dict() for item in selected[:10]],
        "rejected_candidates": [item.to_dict() for item in rejected[:10]],
    }
    return [item.url for item in selected], metrics


def _result_content(result: Any) -> str:
    values: list[str] = []
    for name in ("cleaned_html", "html", "markdown", "fit_markdown", "text"):
        value = getattr(result, name, None)
        if value:
            values.append(str(value))
    return "\n".join(values)[:2_000_000].lower()


def assess_llm_eligibility(result: Any, job_url: str) -> tuple[bool, str]:
    """Reject obvious navigation/error pages before consuming local GPU inference."""
    url_assessment = assess_job_candidate_url(job_url)
    if url_assessment.hard_reject:
        return False, "candidate URL is a navigation or asset URL"

    quality = assess_crawl_result(result)
    if quality.blocked:
        return False, f"page quality rejected LLM use: {quality.reason}"

    content = _result_content(result)
    if "jobposting" in content and "application/ld+json" in content:
        return True, "schema.org JobPosting evidence"
    if any(marker in content for marker in _NON_JOB_PAGE_SIGNALS if marker != "captcha"):
        signals = [signal for signal in _CONTENT_JOB_SIGNALS if signal in content]
        if len(signals) < 2:
            return False, "page contains an error, access-control, or not-found marker"
    if url_assessment.score >= 8:
        return True, "high-confidence job detail URL"

    signals = [signal for signal in _CONTENT_JOB_SIGNALS if signal in content]
    if len(signals) >= 2:
        return True, f"page contains {len(signals)} independent job signals"
    if url_assessment.score >= 3 and signals:
        return True, "candidate URL and page content both contain job evidence"
    return False, "page lacks sufficient job-detail evidence"


def _normalized_grounding_text(value: Any) -> str:
    decoded = html_module.unescape(str(value or "")).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", decoded).split())


def assess_llm_job_grounding(
    job: JobPosting,
    source_url: str,
    source_result: Any,
) -> tuple[bool, str]:
    """Require an LLM payload to be traceable to the acquired detail document."""
    expected_url = canonicalize_candidate_url(source_url)
    extracted_url = canonicalize_candidate_url(str(job.job_url or source_url))
    if not expected_url or extracted_url != expected_url:
        return False, "LLM job URL does not match the acquired detail URL"

    raw_content = _result_content(source_result)
    rendered_content = visible_text(raw_content)
    evidence_text = _normalized_grounding_text(f"{raw_content}\n{rendered_content}")
    if not evidence_text:
        return False, "acquired detail document contains no grounding text"

    if any(marker in evidence_text for marker in _PLACEHOLDER_JOB_SIGNALS):
        return False, "detail document contains placeholder job data"

    title = _normalized_grounding_text(job.title)
    if len(title) < 3 or title not in evidence_text:
        return False, "LLM title is not present in the acquired detail document"

    grounded_fields: list[str] = []
    ungrounded_fields: list[str] = []
    for field_name in (
        "job_reference",
        "company",
        "location_text",
        "posted_date",
        "compensation_text",
    ):
        normalized = _normalized_grounding_text(getattr(job, field_name, None))
        if len(normalized) < 3:
            continue
        if normalized in evidence_text:
            grounded_fields.append(field_name)
        else:
            ungrounded_fields.append(field_name)

    if ungrounded_fields:
        return False, f"LLM fields are not grounded: {','.join(ungrounded_fields)}"

    page_signals = [signal for signal in _CONTENT_JOB_SIGNALS if signal in raw_content]
    if not grounded_fields and len(page_signals) < 2:
        return False, "LLM payload lacks a second independent page-grounded job signal"

    evidence = ["url", "title", *grounded_fields]
    if page_signals:
        evidence.append(f"page_signals:{len(page_signals)}")
    return True, f"grounded_evidence={','.join(evidence)}"


def promote_trusted_detail_url(
    value: str,
    *,
    platform_hint: str | None = None,
    acquisition_hints: dict[str, str] | None = None,
    page_content: str | None = None,
) -> str:
    """Promote an iCIMS wrapper URL to its rendered tenant document host.

    Promotion is evidence-bound: the original URL must be an iCIMS numeric
    detail route and the replacement host/path must be present in detector
    hints or the rendered page. No endpoint or selector is invented.
    """
    original = str(value or "").strip()
    try:
        parsed = urlsplit(original)
    except ValueError:
        return original
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    platform = str(platform_hint or "").strip().lower()
    if platform not in {"", "icims"}:
        return original
    if hostname != "icims.com" and not hostname.endswith(".icims.com"):
        return original
    if hostname.endswith(".i.icims.com"):
        return original
    detail_match = re.match(r"^(?P<listing>/.+?/jobs|/jobs)/\d+(?:/|$)", parsed.path or "", re.IGNORECASE)
    if not detail_match:
        return original
    listing_path = detail_match.group("listing").rstrip("/")

    candidates: list[str] = []
    for key, candidate in (acquisition_hints or {}).items():
        if key.endswith("url") and str(candidate or "").strip():
            candidates.append(str(candidate).strip())
    normalized_page = html_module.unescape(str(page_content or "")).replace(r"\/", "/")
    for match in re.findall(
        r"(?:https?:)?//[^\s\"'<>\\]+",
        normalized_page,
        flags=re.IGNORECASE,
    ):
        candidate = match.rstrip("),.;]}")
        candidates.append(f"https:{candidate}" if candidate.startswith("//") else candidate)

    for candidate in dict.fromkeys(candidates):
        try:
            hinted = urlsplit(candidate)
        except ValueError:
            continue
        hinted_host = str(hinted.hostname or "").lower().rstrip(".")
        hinted_path = re.sub(r"/{2,}", "/", hinted.path or "").rstrip("/")
        if not hinted_host.endswith(".i.icims.com"):
            continue
        if hinted.scheme.lower() not in {"http", "https"}:
            continue
        if hinted_path != listing_path:
            continue
        query = urlencode(
            [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() != "in_iframe"],
            doseq=True,
        )
        return urlunsplit(("https", hinted.netloc.lower(), parsed.path, query, ""))
    return original


def assess_certification_job(job: JobPosting, source_url: str) -> tuple[bool, str]:
    """Stricter certification validator; production's backward-compatible validator remains unchanged."""
    title = normalize_evidence_text(job.title)
    summary = normalize_evidence_text(job.summary)
    detail_text = " | ".join(
        [
            summary,
            *(normalize_evidence_text(value) for value in job.responsibilities),
            *(normalize_evidence_text(value) for value in job.required_skills),
            *(normalize_evidence_text(value) for value in job.preferred_skills),
        ]
    )
    title_rejection = job_title_context_rejection_reason(title, detail_text)
    if title_rejection is not None:
        return False, f"navigation/non-role title rejected: {title_rejection}"
    source_title_rejection = job_title_source_rejection_reason(
        title,
        job_url=job.job_url or source_url,
        job_reference=job.job_reference,
        summary=detail_text,
    )
    if source_title_rejection is not None:
        return False, f"site/non-role title rejected: {source_title_rejection}"

    url = str(job.job_url or source_url or "").strip()
    url_assessment = assess_job_candidate_url(url)
    if url_assessment.hard_reject:
        return False, "job URL is a navigation or asset URL"
    blocked_navigation = {
        "about",
        "benefits",
        "candidate",
        "candidates",
        "community",
        "culture",
        "employers",
        "locations",
        "recruiters",
        "search",
    }
    navigation_tokens = {
        token
        for reason in url_assessment.reasons
        if reason.startswith("navigation_tokens:")
        for token in reason.partition(":")[2].split(",")
    }
    if url_assessment.score < 8 and navigation_tokens & blocked_navigation:
        return False, (
            "job URL resolves to a navigation/marketing route: "
            + ",".join(sorted(navigation_tokens & blocked_navigation))
        )

    quality_score = 3
    evidence: list[str] = ["title"]
    if url_assessment.score >= 8:
        quality_score += 3
        evidence.append("detail_url")
    elif url_assessment.score >= 3:
        quality_score += 1
        evidence.append("plausible_url")

    detail_signals = job_detail_signal_count(detail_text)
    if len(summary) >= 80:
        quality_score += 2
        evidence.append("summary")
    elif len(summary) >= 30:
        quality_score += 1
        evidence.append("short_summary")
    if job.responsibilities or job.required_skills or job.preferred_skills:
        quality_score += 2
        evidence.append("requirements")
    if job.location_text:
        quality_score += 1
        evidence.append("location")
    if job.job_reference or job.posted_date:
        quality_score += 1
        evidence.append("reference_or_date")
    if job.company:
        quality_score += 1
        evidence.append("company")

    if detail_signals:
        quality_score += min(2, detail_signals)
        evidence.append(f"detail_signals:{detail_signals}")
    if (
        url_assessment.score < 3
        and not (job.responsibilities or job.required_skills or job.preferred_skills)
        and detail_signals < 2
    ):
        return False, "unfamiliar URL lacks independent rendered job-detail evidence"
    if has_marketing_page_language(f"{title} {summary}") and detail_signals < 2:
        return False, "marketing copy is not an individual job detail"

    if quality_score < 6:
        return False, f"insufficient independent job evidence ({','.join(evidence)})"
    return True, f"quality_score={quality_score};evidence={','.join(evidence)}"
