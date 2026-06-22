from __future__ import annotations

import pytest

from src.portals.detector import detect_portal
from src.portals.safety import PortalUrlSafetyError, host_is_allowed, validate_public_http_url


def test_rejects_private_and_loopback_urls() -> None:
    for url in ("http://127.0.0.1:8000", "http://localhost", "http://10.0.0.10"):
        with pytest.raises(PortalUrlSafetyError):
            validate_public_http_url(url)


def test_accepts_public_ip_and_preserves_fragment() -> None:
    result = validate_public_http_url("https://8.8.8.8/jobs#openings")
    assert result.hostname == "8.8.8.8"
    assert result.normalized_url == "https://8.8.8.8/jobs#openings"


def test_host_allow_list_supports_exact_and_subdomain_patterns() -> None:
    assert host_is_allowed("jobs.example.com", ["jobs.example.com"])
    assert host_is_allowed("careers.example.com", ["*.example.com"])
    assert not host_is_allowed("example.com", ["*.example.com"])
    assert not host_is_allowed("not-example.com", ["*.example.com"])


def test_detects_workday_and_protection_signals() -> None:
    workday = detect_portal(
        listing_url="https://acme.myworkdayjobs.com/en-US/careers",
        html="<title>Acme Careers</title>",
    )
    assert workday.source_platform == "workday"
    assert workday.profile_name == "workday"

    protected = detect_portal(
        listing_url="https://careers.example.com/jobs",
        html="<title>Just a moment...</title><p>Verify you are human</p>",
    )
    assert protected.blocked is True
    assert protected.source_platform == "blocked_or_protected"
