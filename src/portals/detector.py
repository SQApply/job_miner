from __future__ import annotations

import hashlib
import html as html_module
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit


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


def _embedded_urls(value: str) -> list[str]:
    normalized = html_module.unescape(str(value or "")).replace(r"\/", "/")
    patterns = (
        r"https?://(?:boards|job-boards)\.greenhouse\.io/[^\s\"'<>]+",
        r"https?://jobs(?:\.eu)?\.lever\.co/[^\s\"'<>]+",
        r"https?://jobs\.ashbyhq\.com/[^\s\"'<>]+",
        r"https?://[a-z0-9-]+(?:\.wd\d+)?\.myworkdayjobs\.com/[^\s\"'<>]+",
    )
    urls: list[str] = []
    for pattern in patterns:
        urls.extend(match.rstrip(").,;]") for match in re.findall(pattern, normalized, flags=re.IGNORECASE))
    return list(dict.fromkeys(urls))


def _acquisition_signature(listing_url: str, page_content: str) -> tuple[str, dict[str, str]] | None:
    candidates = [listing_url, *_embedded_urls(page_content)]
    for candidate in candidates:
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
    acquisition = _acquisition_signature(listing_url, f"{html_value}\n{text_content or ''}")

    blocked_markers = (
        "captcha", "verify you are human", "access denied", "cf-chl-", "cloudflare ray id",
        "unusual traffic", "robot check",
    )
    if _contains_any(combined, blocked_markers):
        return PortalDetection(
            source_platform="blocked_or_protected",
            profile_name="generic_listing",
            crawl_strategy="generic_listing",
            confidence=0.99,
            requires_review=True,
            reasons=["The listing page contains an access-control, CAPTCHA, or bot-protection indicator."],
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
        )

    return PortalDetection(
        source_platform="custom_listing",
        profile_name="generic_listing",
        crawl_strategy="generic_listing",
        confidence=0.50,
        requires_review=True,
        reasons=["No known ATS signature was found; the generic listing profile will be tested."],
        page_title=_page_title(html_value),
        content_fingerprint=fingerprint,
    )
