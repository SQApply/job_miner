import json

from src.extract.model_lane import parse_extracted_jobs


def test_parse_extracted_jobs_single_dict():
    payload = json.dumps({"title": "Data Engineer", "job_url": "https://example.com/jobs/1"})
    job = parse_extracted_jobs(payload, "https://example.com/jobs/1")
    assert job is not None
    assert job.title == "Data Engineer"