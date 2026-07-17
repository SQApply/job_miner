from __future__ import annotations

import html as html_module
import ipaddress
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit


ROUTE_RESOLUTION_CONTRACT_VERSION = "1.0"

_STRONG_LISTING_LABELS = (
    "browse jobs",
    "current jobs",
    "current openings",
    "find a job",
    "find jobs",
    "job listings",
    "job openings",
    "open positions",
    "search careers",
    "search jobs",
    "see all jobs",
    "view all jobs",
    "view jobs",
)

_WEAK_LISTING_LABELS = (
    "careers",
    "career opportunities",
    "employment opportunities",
    "join our team",
    "opportunities",
    "work with us",
)

_NEGATIVE_ROUTE_TOKENS = {
    "about",
    "accessibility",
    "benefits",
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

_NON_ROUTE_HOSTS = (
    "doubleclick.net",
    "facebook.com",
    "google-analytics.com",
    "google.com",
    "googletagmanager.com",
    "hcaptcha.com",
    "instagram.com",
    "linkedin.com",
    "newrelic.com",
    "recaptcha.net",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtube.com",
)

_LISTING_PATH_PATTERN = re.compile(
    r"(?:^|/)(?:career(?:s)?|employment|find-work|job-search|jobs?|openings|"
    r"opportunities|search-jobs|search-results[^/]*|vacancies)(?:/|$)",
    flags=re.IGNORECASE,
)

_DETAIL_PATH_PATTERN = re.compile(
    r"/jobs?/details?(?:/|$)|"
    r"/jobs?/\d+(?:[-_/]|$)|"
    r"/(?:job|position|posting|requisition)/[^/]+|"
    r"/(?:jb|job-board)/[^/]+/\d+(?:/|$)|"
    r"/(?:view|show)[-_]?job(?:[_.-]|/|$)",
    flags=re.IGNORECASE,
)

_DETAIL_QUERY_KEYS = {
    "gh_jid",
    "job",
    "job_id",
    "jobid",
    "jid",
    "posting_id",
    "postingid",
    "reqid",
    "requisitionid",
}

_JOB_QUERY_KEYS = {
    "careerid",
    "companyid",
    "jobboard",
    "jobsearch",
    "searchkeyword",
    "searchquery",
    "site",
}

_PAGINATION_QUERY_KEYS = {
    "currentpage",
    "offset",
    "p",
    "page",
    "pageno",
    "pagenumber",
    "pg",
    "start",
}

_CONFIG_URL_PATTERN = re.compile(
    r"(?is)(?:career|jobs?|openings?|recruit(?:ing)?)[a-z0-9_-]{0,40}"
    r"(?:url|uri|href|link|endpoint|board|site)?\s*[:=]\s*[\"']"
    r"(?P<url>https?://[^\"'<>\s]+)"
)

_ABSOLUTE_URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+", flags=re.IGNORECASE)


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", str(value or "").lower())
        if token
    }


def _normalized_host(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _is_public_host_literal(hostname: str) -> bool:
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        return True


def _strip_www(hostname: str) -> str:
    host = _normalized_host(hostname)
    return host[4:] if host.startswith("www.") else host


def hosts_are_related(first: str, second: str) -> bool:
    """Conservatively recognize exact, www, and parent/subdomain ownership.

    This intentionally avoids comparing only the final two labels because that
    would incorrectly treat unrelated hosts under public suffixes such as
    ``co.uk`` as the same organization.
    """
    left = _strip_www(first)
    right = _strip_www(second)
    if not left or not right:
        return False
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def _host_matches(hostname: str, suffix: str) -> bool:
    host = _normalized_host(hostname)
    normalized_suffix = _normalized_host(suffix)
    return host == normalized_suffix or host.endswith(f".{normalized_suffix}")


def _non_route_host(hostname: str) -> bool:
    return any(_host_matches(hostname, suffix) for suffix in _NON_ROUTE_HOSTS)


def _provider_platform(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    hostname = _normalized_host(parsed.hostname or "")
    if not hostname:
        return None
    if hostname.endswith("myworkdayjobs.com"):
        return "workday"
    if hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        return "greenhouse"
    if hostname in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return "lever"
    if hostname == "jobs.ashbyhq.com":
        return "ashby"

    # Lazy import avoids coupling detector initialization to the resolver.
    from .detector import known_browser_ats_platform

    return known_browser_ats_platform(url)


def _normalize_route_url(source_url: str, value: Any) -> str:
    raw = html_module.unescape(str(value or "")).replace(r"\/", "/").strip()
    if not raw or len(raw) > 4096:
        return ""
    try:
        parsed = urlsplit(urljoin(source_url, raw))
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    try:
        if parsed.port not in {None, 80, 443}:
            return ""
    except ValueError:
        return ""
    hostname = _normalized_host(parsed.hostname)
    if not hostname or not _is_public_host_literal(hostname) or _non_route_host(hostname):
        return ""

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query = parsed.query
    fragment = parsed.fragment.strip().rstrip("/")
    if fragment and not (_tokens(fragment) & {"career", "careers", "job", "jobs", "openings"}):
        fragment = ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, fragment))


class _RouteEvidenceParser(HTMLParser):
    def __init__(self, *, max_items: int = 2_000) -> None:
        super().__init__(convert_charrefs=True)
        self.max_items = max_items
        self._anchor: dict[str, str] | None = None
        self._anchor_text: list[str] = []
        self.anchors: list[dict[str, str]] = []
        self.iframes: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.scripts: list[dict[str, str]] = []
        self.config_values: list[dict[str, str]] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): _compact(value) for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attributes(attrs)
        lowered = tag.lower()
        if lowered == "a" and len(self.anchors) < self.max_items:
            self._anchor = {
                "url": values.get("href", ""),
                "label": values.get("title", "") or values.get("aria-label", ""),
            }
            self._anchor_text = []
        elif lowered == "iframe" and len(self.iframes) < self.max_items:
            self.iframes.append(
                {
                    "url": values.get("src", ""),
                    "label": values.get("title", "") or values.get("aria-label", ""),
                }
            )
        elif lowered == "form" and len(self.forms) < self.max_items:
            self.forms.append(
                {
                    "url": values.get("action", ""),
                    "label": values.get("aria-label", "") or values.get("name", ""),
                }
            )
        elif lowered == "script" and len(self.scripts) < self.max_items:
            self.scripts.append({"url": values.get("src", ""), "label": ""})

        for key, value in values.items():
            if len(self.config_values) >= self.max_items:
                break
            if not value:
                continue
            key_tokens = _tokens(key)
            if (
                key_tokens & {"career", "careers", "job", "jobs", "openings", "recruiting"}
                and key_tokens & {"action", "href", "link", "src", "url"}
            ):
                self.config_values.append({"url": value, "label": key})

    def handle_data(self, data: str) -> None:
        if self._anchor is None:
            return
        value = _compact(data)
        if value:
            self._anchor_text.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._anchor is None:
            return
        text = _compact(" ".join(self._anchor_text))
        self.anchors.append(
            {**self._anchor, "label": text or self._anchor.get("label", "")}
        )
        self._anchor = None
        self._anchor_text = []


def _structured_link_rows(value: Any, *, max_items: int = 2_000) -> Iterable[dict[str, str]]:
    if not isinstance(value, dict):
        return []
    rows: list[dict[str, str]] = []
    for bucket in ("internal", "external"):
        values = value.get(bucket)
        if not isinstance(values, (list, tuple)):
            continue
        for item in values:
            if len(rows) >= max_items:
                return rows
            if isinstance(item, str):
                rows.append({"url": item, "label": ""})
                continue
            if isinstance(item, dict):
                url = item.get("href") or item.get("url")
                label = item.get("text") or item.get("title") or ""
            else:
                url = getattr(item, "href", None) or getattr(item, "url", None)
                label = getattr(item, "text", None) or getattr(item, "title", None) or ""
            if url:
                rows.append({"url": str(url), "label": _compact(label)})
    return rows


@dataclass(frozen=True)
class RouteCandidate:
    url: str
    hostname: str
    platform: str | None
    route_kind: str
    score: int
    confidence: str
    trusted: bool
    cross_domain: bool
    sources: tuple[str, ...]
    labels: tuple[str, ...]
    reasons: tuple[str, ...]
    trusted_hosts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("sources", "labels", "reasons", "trusted_hosts"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class ListingRouteResolution:
    source_url: str
    selected: RouteCandidate | None
    candidates: tuple[RouteCandidate, ...]
    rejected_candidates: int
    ambiguous: bool
    reason: str
    strategy: str = "evidence_bound_listing_route_v1"
    contract_version: str = ROUTE_RESOLUTION_CONTRACT_VERSION

    @property
    def trusted_hosts(self) -> tuple[str, ...]:
        return self.selected.trusted_hosts if self.selected else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "strategy": self.strategy,
            "source_url": self.source_url,
            "selected": self.selected.to_dict() if self.selected else None,
            "candidates": [candidate.to_dict() for candidate in self.candidates[:25]],
            "rejected_candidates": self.rejected_candidates,
            "ambiguous": self.ambiguous,
            "reason": self.reason,
            "trusted_hosts": list(self.trusted_hosts),
        }


@dataclass
class _EvidenceAccumulator:
    url: str
    sources: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)


def _is_pagination_only(source_url: str, candidate_url: str) -> bool:
    try:
        source = urlsplit(source_url)
        candidate = urlsplit(candidate_url)
    except ValueError:
        return False
    if (
        _normalized_host(source.hostname or "") != _normalized_host(candidate.hostname or "")
        or re.sub(r"/{2,}", "/", source.path or "/").rstrip("/")
        != re.sub(r"/{2,}", "/", candidate.path or "/").rstrip("/")
    ):
        return False
    changed_keys = {key.lower() for key, _ in parse_qsl(candidate.query, keep_blank_values=True)}
    source_keys = {key.lower() for key, _ in parse_qsl(source.query, keep_blank_values=True)}
    return bool(changed_keys) and changed_keys <= (_PAGINATION_QUERY_KEYS | source_keys)


def _listing_path_strength(path: str) -> int:
    normalized = re.sub(r"/{2,}", "/", str(path or "/").lower()).rstrip("/") or "/"
    if re.search(
        r"(?:^|/)(?:job-search|search-jobs|search-results[^/]*|job-openings|open-positions)(?:/|$)",
        normalized,
    ):
        return 3
    if re.search(r"(?:^|/)(?:jobs|openings|vacancies)(?:/|$)", normalized):
        return 2
    if _LISTING_PATH_PATTERN.search(normalized):
        return 1
    return 0


def _looks_like_detail_route(path: str, query_keys: set[str], fragment: str) -> bool:
    normalized_path = re.sub(r"/{2,}", "/", str(path or "/"))
    if _DETAIL_PATH_PATTERN.search(normalized_path) or query_keys & _DETAIL_QUERY_KEYS:
        return True

    # Common custom boards use /jobs/<slug>-12345, /jobs/12345-title, or a
    # numeric hash route. These are detail pages even though the segment is not
    # purely numeric, and must never become the fleet's canonical listing URL.
    parts = [part for part in normalized_path.split("/") if part]
    lowered_parts = [part.lower() for part in parts]
    for index, part in enumerate(parts[:-1]):
        if part.lower() not in {"job", "jobs", "position", "positions"}:
            continue
        identifier = parts[index + 1]
        if re.search(r"(?:^\d{4,}(?:[-_]|$)|[-_]\d{4,}(?:[-_]|$))", identifier):
            return True
        if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identifier, re.I):
            return True
    if any(part.startswith("viewjob") for part in lowered_parts):
        return True

    fragment_value = str(fragment or "").lower().lstrip("!/")
    if re.search(r"(?:^|/)jobs?/\d{4,}(?:/|$)", fragment_value):
        return True
    return False


def _score_candidate(
    accumulator: _EvidenceAccumulator,
    *,
    source_url: str,
    page_job_evidence: int,
) -> RouteCandidate | None:
    try:
        source = urlsplit(source_url)
        parsed = urlsplit(accumulator.url)
    except ValueError:
        return None
    source_host = _normalized_host(source.hostname or "")
    hostname = _normalized_host(parsed.hostname or "")
    if not hostname or _is_pagination_only(source_url, accumulator.url):
        return None

    path = parsed.path or "/"
    route_text = f"{path} {parsed.query} {parsed.fragment}".lower()
    route_tokens = _tokens(route_text)
    label_text = " ".join(sorted(accumulator.labels)).lower()
    sources = set(accumulator.sources)
    platform = _provider_platform(accumulator.url)
    related = hosts_are_related(source_host, hostname)
    cross_domain = hostname != source_host
    listing_path = bool(_LISTING_PATH_PATTERN.search(path))
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    detail_path = _looks_like_detail_route(path, query_keys, parsed.fragment)
    hash_listing = bool(_tokens(parsed.fragment) & {"career", "careers", "job", "jobs", "openings"})
    strong_label = any(label in label_text for label in _STRONG_LISTING_LABELS)
    weak_label = any(label in label_text for label in _WEAK_LISTING_LABELS)
    host_listing = hostname.split(".", 1)[0] in {
        "apply",
        "career",
        "careers",
        "jobs",
        "recruiting",
    }

    path_parts = [part.lower() for part in path.split("/") if part]
    provider_detail_path = bool(
        (platform in {"ashby", "lever", "smartrecruiters"} and len(path_parts) >= 2)
        or (platform == "workable" and "j" in path_parts)
        or (
            platform == "bamboohr"
            and "careers" in path_parts
            and len(path_parts) >= 2
            and path_parts[-1].isdigit()
        )
    )
    if detail_path or provider_detail_path:
        return None

    source_strength = _listing_path_strength(source.path)
    candidate_strength = _listing_path_strength(path)
    if (
        hostname == source_host
        and source_strength >= 2
        and candidate_strength <= source_strength
        and not platform
        and not ({"iframe", "config", "redirect"} & sources)
    ):
        # Once already on a strong listing/results route, do not bounce to a
        # peer locale or navigation route. The current document should be
        # harvested; equal-strength routes caused the observed fleet loops.
        return None
    negative = route_tokens & _NEGATIVE_ROUTE_TOKENS
    if negative and not strong_label and not platform and not ({"iframe", "redirect"} & sources):
        return None

    score = 0
    reasons: list[str] = []
    if platform:
        score += 16
        reasons.append(f"known_platform:{platform}")
    if "iframe" in sources:
        score += 12
        reasons.append("embedded_iframe")
    if "redirect" in sources:
        score += 8
        reasons.append("http_redirect")
        if page_job_evidence >= 2:
            score += 6
            reasons.append("redirected_job_surface")
    if "config" in sources:
        score += 7
        reasons.append("job_configuration")
    if "form" in sources:
        score += 4
        reasons.append("form_action")
    if "structured_link" in sources:
        score += 2
        reasons.append("rendered_link")
    if "anchor" in sources:
        score += 2
        reasons.append("anchor_link")
    if strong_label:
        score += 12
        reasons.append("strong_listing_label")
    elif weak_label:
        score += 5
        reasons.append("weak_listing_label")
    if listing_path:
        score += 9
        reasons.append("listing_path")
    if query_keys & _JOB_QUERY_KEYS:
        score += 4
        reasons.append("job_query")
    if hash_listing:
        score += 6
        reasons.append("job_hash_route")
    if host_listing:
        score += 5
        reasons.append("listing_hostname")
    if hostname == source_host:
        score += 5
        reasons.append("same_host")
    elif related:
        score += 4
        reasons.append("related_host")
    else:
        score -= 4
        reasons.append("external_host")
    if negative:
        score -= 5
        reasons.append(f"navigation_tokens:{','.join(sorted(negative))}")

    provider_specific_shape = bool(
        platform in {"ashby", "greenhouse", "lever", "workday"}
        or host_listing
        or hostname.startswith("careers-")
        or (platform == "jobdiva" and "/portal" in path.lower())
    )
    provider_route_evidence = bool(
        provider_specific_shape
        or listing_path
        or strong_label
        or "iframe" in sources
        or "redirect" in sources
    )
    same_owner_trusted = related and score >= 12 and bool(
        listing_path or strong_label or hash_listing or host_listing or "redirect" in sources
    )
    external_evidence = bool(
        ("iframe" in sources and (listing_path or host_listing or platform))
        or (strong_label and (listing_path or host_listing))
        or (
            "redirect" in sources
            and (listing_path or host_listing or platform or page_job_evidence >= 2)
        )
        or (
            len(sources) >= 2
            and (listing_path or host_listing)
        )
    )
    provider_trusted = bool(platform and provider_route_evidence and score >= 18)
    external_trusted = bool(not related and external_evidence and score >= 18)
    trusted = provider_trusted or same_owner_trusted or external_trusted

    if platform:
        route_kind = "known_ats"
    elif hostname == source_host:
        route_kind = "same_host_listing"
    elif related:
        route_kind = "related_host_listing"
    else:
        route_kind = "evidence_bound_external_listing"
    confidence = "high" if score >= 24 else "medium" if score >= 18 else "low"
    return RouteCandidate(
        url=accumulator.url,
        hostname=hostname,
        platform=platform,
        route_kind=route_kind,
        score=score,
        confidence=confidence,
        trusted=trusted,
        cross_domain=cross_domain,
        sources=tuple(sorted(sources)),
        labels=tuple(sorted(label for label in accumulator.labels if label))[:10],
        reasons=tuple(reasons),
        trusted_hosts=(hostname,) if trusted else (),
    )


def resolve_listing_route(
    *,
    source_url: str,
    html: str | None = None,
    structured_links: Any = None,
    final_url: str | None = None,
    max_candidates: int = 500,
) -> ListingRouteResolution:
    """Select a listing/ATS route using only evidence present in the acquired surface.

    The function performs no network requests and never invents an endpoint. DNS
    and public-address validation remain mandatory at the caller before a trusted
    route is fetched.
    """
    normalized_source = _normalize_route_url(source_url, source_url)
    if not normalized_source:
        return ListingRouteResolution(
            source_url=str(source_url or ""),
            selected=None,
            candidates=(),
            rejected_candidates=0,
            ambiguous=False,
            reason="source URL is not a valid public HTTP(S) route",
        )

    accumulators: dict[str, _EvidenceAccumulator] = {}
    rejected = 0

    def add(value: Any, source: str, label: Any = "") -> None:
        nonlocal rejected
        if len(accumulators) >= max(1, int(max_candidates)):
            return
        normalized = _normalize_route_url(normalized_source, value)
        if not normalized or normalized.rstrip("/") == normalized_source.rstrip("/"):
            if value:
                rejected += 1
            return
        item = accumulators.setdefault(normalized, _EvidenceAccumulator(url=normalized))
        item.sources.add(source)
        compact_label = _compact(label).lower()
        if compact_label:
            item.labels.add(compact_label[:500])

    raw_html = html_module.unescape(str(html or "")[:2_000_000]).replace(r"\/", "/")
    raw_html = re.sub(r"\\u002[fF]", "/", raw_html)
    raw_html = re.sub(r"\\u003[aA]", ":", raw_html)
    raw_html = re.sub(r"\\u0026", "&", raw_html, flags=re.IGNORECASE)
    parser = _RouteEvidenceParser(max_items=min(max(1, int(max_candidates)) * 4, 2_000))
    try:
        parser.feed(raw_html)
    except Exception:
        pass

    for item in parser.anchors:
        add(item.get("url"), "anchor", item.get("label"))
    for item in parser.iframes:
        add(item.get("url"), "iframe", item.get("label"))
    for item in parser.forms:
        add(item.get("url"), "form", item.get("label"))
    for item in parser.config_values:
        add(item.get("url"), "config", item.get("label"))
    for item in _structured_link_rows(structured_links, max_items=2_000):
        add(item.get("url"), "structured_link", item.get("label"))

    for match in _CONFIG_URL_PATTERN.finditer(raw_html):
        add(match.group("url"), "config", "job configuration")
    for match in _ABSOLUTE_URL_PATTERN.finditer(raw_html):
        candidate = match.group(0).rstrip("),.;]}")
        if _provider_platform(candidate):
            add(candidate, "config", "known ATS URL")

    if final_url:
        normalized_final = _normalize_route_url(normalized_source, final_url)
        if normalized_final and normalized_final.rstrip("/") != normalized_source.rstrip("/"):
            add(normalized_final, "redirect", "redirect destination")

    page_lower = raw_html.lower()
    page_job_evidence = sum(
        1
        for marker in (
            "job description",
            "job openings",
            "open positions",
            "search jobs",
            "view jobs",
        )
        if marker in page_lower
    )
    candidates = [
        candidate
        for accumulator in accumulators.values()
        if (candidate := _score_candidate(
            accumulator,
            source_url=normalized_source,
            page_job_evidence=page_job_evidence,
        ))
        is not None
    ]
    candidates.sort(
        key=lambda item: (
            not item.trusted,
            -item.score,
            0 if item.platform else 1,
            -len(item.sources),
            item.url,
        )
    )
    trusted = [candidate for candidate in candidates if candidate.trusted]
    ambiguous = False
    selected: RouteCandidate | None = trusted[0] if trusted else None
    if selected and len(trusted) > 1:
        runner_up = trusted[1]
        unrelated_destinations = not hosts_are_related(selected.hostname, runner_up.hostname)
        if (
            unrelated_destinations
            and not selected.platform
            and not runner_up.platform
            and selected.score - runner_up.score <= 2
        ):
            selected = None
            ambiguous = True

    if selected:
        reason = (
            f"selected {selected.route_kind} with score {selected.score} from "
            f"{','.join(selected.sources)}"
        )
    elif ambiguous:
        reason = "multiple unrelated listing destinations have equivalent evidence"
    elif candidates:
        reason = "route candidates were found but none met the trust threshold"
    else:
        reason = "no evidence-bound listing route was found"
    return ListingRouteResolution(
        source_url=normalized_source,
        selected=selected,
        candidates=tuple(candidates),
        rejected_candidates=rejected,
        ambiguous=ambiguous,
        reason=reason,
    )


def route_acquisition_hints(resolution: ListingRouteResolution) -> dict[str, str]:
    selected = resolution.selected
    if selected is None:
        return {}
    return {
        "listing_url": selected.url,
        "resolved_route_kind": selected.route_kind,
        "resolved_route_score": str(selected.score),
        "resolved_route_sources": ",".join(selected.sources),
        "resolved_route_trusted_host": selected.hostname,
    }


def route_host_is_trusted(resolution: ListingRouteResolution, hostname: str) -> bool:
    host = _normalized_host(hostname)
    return bool(host and host in resolution.trusted_hosts)
