from __future__ import annotations

"""Helpers for normalising source-provided job posting dates.

Job boards expose freshness in many formats (for example, ``3 days ago``,
``Yesterday`` or an ISO date).  The API keeps the raw value for transparency
and stores a best-effort UTC timestamp only when it can be parsed safely.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

UTC = timezone.utc

_RELATIVE_DATE_PATTERN = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>minute|hour|day|week|month|year)s?\s*(?:ago|old)?",
    re.IGNORECASE,
)


def ensure_utc(value: datetime) -> datetime:
    """Return an aware datetime normalised to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _start_of_day(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=UTC)


def _parse_absolute_date(value: str) -> datetime | None:
    """Parse common job-board date formats without adding a new dependency."""
    candidate = value.strip()
    if not candidate:
        return None

    iso_candidate = candidate.replace("Z", "+00:00")
    try:
        return ensure_utc(datetime.fromisoformat(iso_candidate))
    except ValueError:
        pass

    date_formats = (
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
    )
    for date_format in date_formats:
        try:
            return datetime.strptime(candidate, date_format).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_job_posted_at(value: Any, *, reference_time: datetime | None = None) -> datetime | None:
    """Convert a source-provided posting date into a UTC timestamp where possible.

    ``None`` is deliberately returned for ambiguous or unsupported strings.
    The caller should then use the warehouse ``first_seen_at`` timestamp as a
    clearly labelled fallback rather than silently inventing a posting date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)

    raw = str(value).strip()
    if not raw:
        return None

    now = ensure_utc(reference_time or datetime.now(UTC))
    lowered = " ".join(raw.lower().split())

    if lowered in {"today", "posted today", "just posted", "just now", "new"}:
        return _start_of_day(now)
    if lowered in {"yesterday", "posted yesterday"}:
        return _start_of_day(now - timedelta(days=1))

    relative_match = _RELATIVE_DATE_PATTERN.search(lowered)
    if relative_match:
        count = int(relative_match.group("count"))
        unit = relative_match.group("unit").lower()
        if unit == "minute":
            return now - timedelta(minutes=count)
        if unit == "hour":
            return now - timedelta(hours=count)
        if unit == "day":
            return _start_of_day(now - timedelta(days=count))
        if unit == "week":
            return _start_of_day(now - timedelta(weeks=count))
        if unit == "month":
            return _start_of_day(now - timedelta(days=count * 30))
        if unit == "year":
            return _start_of_day(now - timedelta(days=count * 365))

    return _parse_absolute_date(raw)
