from __future__ import annotations

import hashlib
import html as html_module
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from .page_quality import assess_page_quality


KNOWN_BROWSER_ATS_HOSTS: dict[str, tuple[str, ...]] = {
    "workable": ("app.workable.com", "apply.workable.com", "jobs.workable.com"),
    "smartrecruiters": ("jobs.smartrecruiters.com",),
    "icims": ("icims.com",),
    "jobvite": ("jobs.jobvite.com",),
    "oracle_recruiting": ("taleo.net", "oraclecloud.com"),
    "successfactors": ("successfactors.com",),
    "dayforce": ("jobs.dayforcehcm.com",),
    "ukg": ("recruiting.ultipro.com", "recruiting2.ultipro.com"),
    "adp": ("workforcenow.adp.com", "jobs.adp.com"),
    "bamboohr": ("bamboohr.com",),
    "paylocity": ("recruiting.paylocity.com",),
}


_STRONG_LISTING_LABELS = (
    "browse jobs",
    "current openings",
    "find a job",
    "find jobs",
    "job openings",
    "open positions",
    "search jobs",
    "see jobs",
    "view jobs",
)


class _ListingLinkParser(HTMLParser):
    """Collect bounded, rendered navigation evidence without CSS/XPath rules."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        lowered = tag.lower()
        if lowered == "a":
            self._href = attributes.get("href", "").strip() or None
            self._text = []
        elif lowered == "iframe":
            src = attributes.get("src", "").strip()
            if src:
                self.links.append((src, "iframe"))

    def handle_data(self, data: str) -> None:
        if self._href:
            value = " ".join(data.split())
            if value:
                self._text.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        self.links.append((self._href, " ".join(self._text)))
        self._href = None
        self._text = []


@dataclass(frozen=True)
class PortalDetection:
    source_platform: str
    profile_name: str
    crawl_strategy: str
    confidence: float
    requires_review: bool
    reasons: list[str]
    page_title: str | None
    content_fingerprint: str
    blocked: bool = False
    acquisition_hints: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _page_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>\s*(.*?)\s*</title>", html or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip()
    return value[:300] or None


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


def _safe_token(value: Any) -> str | None:
    token = str(value or "").strip().strip("/")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", token):
        return token
    return None


def known_browser_ats_platform(value: str) -> str | None:
    try:
        hostname = (urlsplit(value).hostname or value).lower().rstrip(".")
    except ValueError:
        return None
    for platform, suffixes in KNOWN_BROWSER_ATS_HOSTS.items():
        if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes):
            return platform
    return None


def _same_site(first: str, second: str) -> bool:
    first_parts = str(first or "").lower().rstrip(".").split(".")
    second_parts = str(second or "").lower().rstrip(".").split(".")
    return len(first_parts) >= 2 and len(second_parts) >= 2 and first_parts[-2:] == second_parts[-2:]


def _listing_candidate_score(
    candidate: str,
    *,
    label: str,
    source_url: str,
) -> int | None:
    try:
        parsed = urlsplit(candidate)
        source = urlsplit(source_url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or not source.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        if parsed.port not in {None, 80, 443}:
            return None
    except ValueError:
        return None

    hostname = parsed.hostname.lower().rstrip(".")
    source_host = source.hostname.lower().rstrip(".")
    if hostname != source_host and not _same_site(hostname, source_host):
        # Cross-site ATS links are handled by the existing provider-signature
        # detector. Generic route repair remains same-site only.
        return None

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    lowered_path = path.lower().rstrip("/") or "/"
    normalized_label = " ".join(str(label or "").lower().split())
    numeric_detail = bool(re.search(r"/jobs?/\d+(?:/|$)", lowered_path))
    if numeric_detail:
        return None

    score = 2 if hostname == source_host else 1
    if any(phrase == normalized_label or phrase in normalized_label for phrase in _STRONG_LISTING_LABELS):
        score += 12
    if re.search(r"(?:^|/)(?:job-search|search-jobs|search-results[^/]*|job-openings|open-positions)(?:/|$)", lowered_path):
        score += 10
    elif re.search(r"(?:^|/)(?:jobs|openings|opportunities)(?:/|$)", lowered_path):
        score += 5

    if hostname == "icims.com" or hostname.endswith(".icims.com"):
        if re.search(r"/jobs$", lowered_path):
            score += 10
        if hostname.endswith(".i.icims.com"):
            score += 7
        if len([part for part in lowered_path.split("/") if part]) > 1:
            score += 4

    current = urlunsplit((source.scheme.lower(), source.netloc.lower(), source.path.rstrip("/") or "/", source.query, ""))
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path.rstrip("/") or "/", parsed.query, ""))
    if normalized == current:
        score -= 2
    return score


def infer_listing_url(listing_url: str, page_content: str) -> str | None:
    """Infer a stronger same-site listing route from rendered links and iframes.

    This is deliberately evidence-based: the route must be present in the page
    and strongly resemble a jobs result page. It never invents an endpoint.
    """
    parser = _ListingLinkParser()
    try:
        parser.feed(html_module.unescape(str(page_content or "")))
    except Exception:
        pass

    candidates: list[tuple[str, str]] = [(listing_url, "")]
    candidates.extend(parser.links[:1000])
    candidates.extend((value, "embedded") for value in _embedded_urls(page_content)[:1000])

    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for index, (raw, label) in enumerate(candidates):
        candidate = urljoin(listing_url, str(raw or "").strip())
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        normalized = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/",
                parsed.query,
                "",
            )
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        score = _listing_candidate_score(normalized, label=label, source_url=listing_url)
        if score is not None:
            ranked.append((score, -index, normalized))

    if not ranked:
        return None
    score, _, selected = max(ranked)
    return selected if score >= 12 else None


def _embedded_urls(value: str) -> list[str]:
    normalized = html_module.unescape(str(value or "")).replace(r"\/", "/")
    candidates = re.findall(r"https?://[^\s\"'<>\\]+", normalized, flags=re.IGNORECASE)
    urls: list[str] = []
    for raw in candidates:
        candidate = raw.rstrip(").,;]}")
        hostname = str(urlsplit(candidate).hostname or "").lower()
        api_provider = any(
            (
                hostname.endswith("greenhouse.io"),
                hostname in {"jobs.lever.co", "jobs.eu.lever.co"},
                hostname == "jobs.ashbyhq.com",
                hostname.endswith("myworkdayjobs.com"),
            )
        )
        if api_provider or known_browser_ats_platform(candidate):
            urls.append(candidate)
    return list(dict.fromkeys(urls))


def _acquisition_signature(listing_url: str, page_content: str) -> tuple[str, dict[str, str]] | None:
    inferred_listing = infer_listing_url(listing_url, page_content)
    candidates = [inferred_listing, listing_url, *_embedded_urls(page_content)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        hostname = str(parsed.hostname or "").lower()
        parts = [part for part in parsed.path.split("/") if part]

        if hostname.endswith("greenhouse.io") and hostname != "boards-api.greenhouse.io":
            token = (parse_qs(parsed.query).get("for") or [None])[0]
            if not token and parts and parts[0].lower() != "embed":
                token = parts[0]
            safe = _safe_token(token)
            if safe:
                return "greenhouse", {"listing_url": candidate, "board_token": safe}

        if hostname in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
            safe = _safe_token(parts[0])
            if safe:
                return "lever", {"listing_url": candidate, "site_token": safe}

        if hostname == "jobs.ashbyhq.com" and parts:
            safe = _safe_token(parts[0])
            if safe:
                return "ashby", {"listing_url": candidate, "board_token": safe}

        workday = re.fullmatch(r"(?P<tenant>[a-z0-9-]+)(?:\.wd\d+)?\.myworkdayjobs\.com", hostname)
        if workday and len(parts) >= 2:
            site = _safe_token(parts[1])
            if site:
                return "workday", {
                    "listing_url": candidate,
                    "tenant": workday.group("tenant"),
                    "site_token": site,
                }

        browser_platform = known_browser_ats_platform(candidate)
        if browser_platform:
            return browser_platform, {"listing_url": candidate}
    return None


def detect_portal(*, listing_url: str, html: str | None, text_content: str | None = None) -> PortalDetection:
    """Classify a public listing page into an existing scraping profile.

    This is intentionally heuristic. The test-scrape stage, rather than the URL
    signature alone, is the final source of truth for whether a portal works.
    """
    html_value = str(html or "")
    combined = f"{listing_url}\n{html_value}\n{text_content or ''}".lower()
    hostname = (urlsplit(listing_url).hostname or "").lower()
    fingerprint = hashlib.sha256(html_value.encode("utf-8", errors="ignore")).hexdigest()
    page_content = f"{html_value}\n{text_content or ''}"
    inferred_listing = infer_listing_url(listing_url, page_content)
    listing_hints = {"listing_url": inferred_listing} if inferred_listing else {}
    acquisition = _acquisition_signature(listing_url, page_content)

    page_quality = assess_page_quality(html=html_value, text_content=text_content)
    if page_quality.blocked:
        return PortalDetection(
            source_platform="blocked_or_protected",
            profile_name="generic_listing",
            crawl_strategy="generic_listing",
            confidence=0.99,
            requires_review=True,
            reasons=[
                "The listing page is an access-control or bot-protection surface.",
                str(page_quality.reason or "Access-control evidence was detected."),
            ],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            blocked=True,
        )

    if (acquisition and acquisition[0] == "workday") or "myworkdayjobs.com" in hostname or "workday" in hostname or "workday" in combined:
        return PortalDetection(
            source_platform="workday",
            profile_name="workday",
            crawl_strategy="workday",
            confidence=0.94,
            requires_review=True,
            reasons=["Workday URL or page signature detected.", "A test scrape is required because Workday deployments vary."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints=acquisition[1] if acquisition and acquisition[0] == "workday" else {},
        )

    if "jobdiva" in hostname or "jobdiva" in combined:
        return PortalDetection(
            source_platform="jobdiva",
            profile_name="jobdiva",
            crawl_strategy="jobdiva",
            confidence=0.96,
            requires_review=True,
            reasons=["JobDiva signature detected.", "A test scrape is required because detail navigation can vary."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
        )

    if (acquisition and acquisition[0] == "greenhouse") or "greenhouse.io" in hostname or "greenhouse" in combined:
        return PortalDetection(
            source_platform="greenhouse",
            profile_name="generic_listing",
            crawl_strategy="generic_listing",
            confidence=0.89,
            requires_review=False,
            reasons=["A Greenhouse public job board URL was detected in the URL or rendered page."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints=acquisition[1] if acquisition and acquisition[0] == "greenhouse" else {},
        )

    if (acquisition and acquisition[0] == "lever") or "jobs.lever.co" in hostname or "lever.co" in hostname:
        return PortalDetection(
            source_platform="lever",
            profile_name="generic_listing",
            crawl_strategy="generic_listing",
            confidence=0.90,
            requires_review=False,
            reasons=["Lever public jobs-board hostname detected."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints=acquisition[1] if acquisition and acquisition[0] == "lever" else {},
        )

    if (acquisition and acquisition[0] == "ashby") or "ashbyhq.com" in hostname or "ashby" in combined:
        return PortalDetection(
            source_platform="ashby",
            profile_name="generic_listing",
            crawl_strategy="generic_listing",
            confidence=0.83,
            requires_review=False,
            reasons=["Ashby-style public job board signature detected."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints=acquisition[1] if acquisition and acquisition[0] == "ashby" else {},
        )

    if acquisition and acquisition[0] in KNOWN_BROWSER_ATS_HOSTS:
        platform = acquisition[0]
        return PortalDetection(
            source_platform=platform,
            profile_name="generic_listing",
            crawl_strategy="generic_listing",
            confidence=0.82,
            requires_review=True,
            reasons=[f"A known {platform} job-board URL was detected and will be tested automatically."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints={**listing_hints, **acquisition[1]},
        )

    if "#" in listing_url or "hash-router" in combined or "hash route" in combined:
        return PortalDetection(
            source_platform="custom_spa",
            profile_name="hash_route_spa",
            crawl_strategy="hash_route_spa",
            confidence=0.74,
            requires_review=True,
            reasons=["Hash-route or SPA navigation signature detected."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints=listing_hints,
        )

    if _contains_any(combined, ("load more jobs", "load more", "show more jobs", "view more jobs")):
        return PortalDetection(
            source_platform="custom_listing",
            profile_name="load_more_button",
            crawl_strategy="load_more_button",
            confidence=0.70,
            requires_review=True,
            reasons=["A load-more style control was detected."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints=listing_hints,
        )

    if "?page=" in listing_url.lower() or re.search(r"(?:rel=[\"']next[\"']|aria-label=[\"'][^\"']*next)", combined):
        return PortalDetection(
            source_platform="custom_listing",
            profile_name="paginated_anchor",
            crawl_strategy="paginated_anchor",
            confidence=0.66,
            requires_review=True,
            reasons=["Pagination signal detected in the listing URL or page markup."],
            page_title=_page_title(html_value),
            content_fingerprint=fingerprint,
            acquisition_hints=listing_hints,
        )

    return PortalDetection(
        source_platform="custom_listing",
        profile_name="generic_listing",
        crawl_strategy="generic_listing",
        confidence=0.50,
        requires_review=True,
        reasons=[
            "No known ATS signature was found; the generic listing profile will be tested.",
            *(["A stronger same-site jobs-listing route was found in rendered navigation."] if inferred_listing else []),
        ],
        page_title=_page_title(html_value),
        content_fingerprint=fingerprint,
        acquisition_hints=listing_hints,
    )
