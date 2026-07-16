import json

from src.extract.model_lane import parse_extracted_jobs
from src.extract.deterministic_lane import extract_job_from_html


def test_parse_extracted_jobs_single_dict():
    payload = json.dumps({"title": "Data Engineer", "job_url": "https://example.com/jobs/1"})
    job = parse_extracted_jobs(payload, "https://example.com/jobs/1")
    assert job is not None
    assert job.title == "Data Engineer"


def test_extract_job_from_json_ld():
    html = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "title": "Senior Data Engineer",
      "datePosted": "2026-07-16",
      "employmentType": ["FULL_TIME", "CONTRACTOR"],
      "identifier": {"@type": "PropertyValue", "value": "REQ-42"},
      "hiringOrganization": {"@type": "Organization", "name": "Acme"},
      "jobLocation": {"address": {"addressLocality": "Chicago", "addressRegion": "IL"}},
      "description": "<p>Build reliable data pipelines.</p>"
    }
    </script></head><body></body></html>
    """
    job = extract_job_from_html(html, "https://example.com/jobs/42")
    assert job is not None
    assert job.title == "Senior Data Engineer"
    assert job.company == "Acme"
    assert job.location_text == "Chicago, IL"
    assert job.job_reference == "REQ-42"
    assert job.summary == "Build reliable data pipelines."
