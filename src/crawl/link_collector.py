from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from ..utils import unique_keep_order


HREF_RE = re.compile(r'''href=["']([^"']+)["']''', re.IGNORECASE)


def _normalized(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl().rstrip("/")


def _text_matches_any(text: str, patterns: list[str]) -> bool:
    text_norm = (text or "").strip().lower()
    return any(p.strip().lower() in text_norm for p in patterns)


def _extract_links_from_html(html: str, page_url: str) -> list[str]:
    links: list[str] = []
    for href in HREF_RE.findall(html or ""):
        links.append(urljoin(page_url, href))
    return links


def collect_job_links(
    result,
    *,
    page_url: str,
    allowed_hosts: list[str],
    href_contains: str,
    detail_text_patterns: list[str],
    exclude_exact_urls: list[str],
) -> list[str]:
    links: list[str] = []
    excluded = {_normalized(url) for url in exclude_exact_urls}
    allowed = set(allowed_hosts)
    page_url_norm = _normalized(page_url)

    buckets = []
    if getattr(result, "links", None):
        buckets.extend((result.links or {}).get("internal", []))
        buckets.extend((result.links or {}).get("external", []))

    for link in buckets:
        href = link.get("href")
        if not href:
            continue

        abs_url = urljoin(page_url, href)
        parsed = urlparse(abs_url)
        if parsed.netloc not in allowed:
            continue

        norm = _normalized(abs_url)
        if norm == page_url_norm or norm in excluded:
            continue

        text = link.get("text") or link.get("title") or ""

        href_match = href_contains in norm if href_contains else True
        text_match = _text_matches_any(text, detail_text_patterns)

        if href_match or text_match:
            links.append(norm)

    html = getattr(result, "cleaned_html", None) or getattr(result, "html", None) or ""
    for abs_url in _extract_links_from_html(html, page_url):
        parsed = urlparse(abs_url)
        if parsed.netloc not in allowed:
            continue

        norm = _normalized(abs_url)
        if norm == page_url_norm or norm in excluded:
            continue

        if href_contains:
            if href_contains in norm:
                links.append(norm)
        else:
            links.append(norm)

    return unique_keep_order(links)


def page_has_load_more(result) -> bool:
    html = getattr(result, "cleaned_html", None) or getattr(result, "html", None) or ""
    return "load more" in html.lower()


def page_has_pagination(result) -> bool:
    html = (getattr(result, "cleaned_html", None) or getattr(result, "html", None) or "").lower()
    numeric_hits = len(re.findall(r'>\s*\d+\s*<', html))
    has_next = any(token in html for token in [">next<", "aria-current", "pagination", "page-item", "page-link", "data-page", "data-next"])
    return numeric_hits >= 2 or has_next


def page_has_multiple_pages(result) -> bool:
    html = (getattr(result, "cleaned_html", None) or getattr(result, "html", None) or "").lower()
    numeric_values = re.findall(r'>\s*(\d+)\s*<', html)
    nums = sorted({int(x) for x in numeric_values if x.isdigit()})
    if len(nums) >= 2:
        return True
    return any(token in html for token in ["next", "aria-current", "page-link", "page-item", "data-page", "data-next"])