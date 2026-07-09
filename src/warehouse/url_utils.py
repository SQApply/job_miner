from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_NAMES = {
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "source",
}


def canonical_job_url(value: str | None) -> str:
    """Return a stable URL key used for incremental rescraping.

    The scraper frequently sees the same job with tracking parameters or small
    URL formatting differences. Keeping a canonical URL prevents duplicate job
    identities and allows listing discovery to decide which detail pages can be
    skipped safely.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw.rstrip("/")

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")

    kept_query: list[tuple[str, str]] = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in _TRACKING_QUERY_NAMES or any(lowered.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
            continue
        kept_query.append((key, val))

    query = urlencode(kept_query, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def canonical_job_urls(values: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = canonical_job_url(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out
