from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlsplit

from .page_quality import PageQualityAssessment, assess_crawl_result


def _link_value(link: Any, name: str) -> str:
    if isinstance(link, dict):
        value = link.get(name)
    else:
        value = getattr(link, name, None)
    return " ".join(str(value or "").split()).strip()


def structured_link_evidence(
    result: Any,
    *,
    max_links: int = 1_000,
    max_chars: int = 500_000,
) -> str:
    """Render Crawl4AI's structured links as bounded detector-only HTML.

    Crawl4AI can retain useful rendered anchors in ``result.links`` even when
    cleaned HTML removes hidden navigation, iframe-shell links, or client-side
    routes. This function exposes only href/text/title evidence to portal
    detection; it does not execute markup or change extraction behavior.
    """
    if max_links < 1 or max_chars < 1:
        return ""
    buckets = getattr(result, "links", None)
    if not isinstance(buckets, dict):
        return ""

    rendered: list[str] = []
    seen: set[tuple[str, str]] = set()
    total_chars = 0
    accepted = 0
    for bucket_name in ("internal", "external"):
        bucket = buckets.get(bucket_name)
        if not isinstance(bucket, (list, tuple)):
            continue
        for link in bucket:
            if accepted >= max_links:
                return "\n".join(rendered)
            href = _link_value(link, "href")
            if not href or len(href) > 4_096:
                continue
            try:
                parsed = urlsplit(href)
            except ValueError:
                continue
            if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
                continue
            if parsed.username is not None or parsed.password is not None:
                continue

            text = _link_value(link, "text") or _link_value(link, "title")
            identity = (href, text)
            if identity in seen:
                continue
            seen.add(identity)
            row = (
                f'<a data-crawl4ai-bucket="{html.escape(bucket_name, quote=True)}" '
                f'href="{html.escape(href, quote=True)}">'
                f"{html.escape(text)}</a>"
            )
            if total_chars + len(row) > max_chars:
                return "\n".join(rendered)
            rendered.append(row)
            total_chars += len(row)
            accepted += 1
    return "\n".join(rendered)


def detection_html(result: Any, primary_html: str | None) -> str:
    evidence = structured_link_evidence(result)
    values = [str(primary_html or "").strip(), evidence.strip()]
    return "\n".join(value for value in values if value)


def result_page_quality(result: Any) -> PageQualityAssessment:
    """Assess raw and rendered Crawl4AI surfaces before route detection."""
    return assess_crawl_result(result)
