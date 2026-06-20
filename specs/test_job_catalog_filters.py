from datetime import datetime, timezone

from src.api.mongo_views import _build_catalog_query, _normalise_freshness, _normalise_work_modes
from src.schemas import JobPosting
from src.warehouse.job_dates import parse_job_posted_at


def test_job_posting_accepts_source_posted_date():
    job = JobPosting.model_validate(
        {
            "title": "AI Engineer",
            "job_url": "https://example.com/jobs/ai-engineer",
            "posted_date": "3 days ago",
        }
    )
    assert job.posted_date == "3 days ago"


def test_parse_relative_posted_date_against_known_reference_time():
    reference = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    parsed = parse_job_posted_at("3 days ago", reference_time=reference)
    assert parsed == datetime(2026, 6, 17, 0, 0, tzinfo=timezone.utc)


def test_parse_absolute_posted_date():
    parsed = parse_job_posted_at("18 Jun 2026")
    assert parsed == datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)


def test_catalog_query_normalises_supported_filter_values():
    query = _build_catalog_query(
        q="python",
        freshness="1w",
        work_modes=["On-site", "remote", "ignored"],
        employment_types=["Full-time"],
        locations=["Bengaluru"],
        companies=["Acme"],
        skills=["Python"],
    )
    assert query["$and"]
    assert _normalise_freshness("1w") == "1w"
    assert _normalise_freshness("not-a-filter") == ""
    assert _normalise_work_modes(["On-site", "REMOTE", "invalid"]) == ["on_site", "remote"]
