from src.warehouse.hashing import make_job_id, stable_hash
from src.warehouse.tower_builders import build_candidate_tower_from_resume_profile, build_job_tower_document


def test_job_hashing_is_stable():
    payload = {"title": "Data Engineer", "job_url": "https://example.com/jobs/1", "company": "Acme"}
    assert make_job_id(payload, target_id="example_jobs") == make_job_id(dict(reversed(list(payload.items()))), target_id="example_jobs")
    assert stable_hash(payload) == stable_hash(dict(reversed(list(payload.items()))))


def test_build_job_tower_document_has_embedding_text():
    job = {"_id": "job_123", "job_id": "job_123", "target_id": "example_jobs", "title": "AWS Data Engineer", "company": "Acme", "location_text": "Remote", "required_skills": ["Python", "AWS", "Spark"], "responsibilities": ["Build ETL pipelines"], "content_hash": "abc"}
    tower = build_job_tower_document(job)
    assert tower.job_id == "job_123"
    assert "AWS Data Engineer" in tower.job_embedding_text
    assert "Python" in tower.job_embedding_text


def test_build_candidate_tower_from_resume_profile_has_embedding_text():
    profile = {"resume_id": "res_123", "sha256": "a" * 64, "source_file_name": "resume.pdf", "contact": {"full_name": "Jane Candidate", "email": "jane@example.com"}, "headline": "Data Scientist", "summary": "Builds machine learning systems.", "primary_skills": ["Python", "Machine Learning"], "experience": [{"title": "Data Scientist", "company": "Acme", "responsibilities": ["Built ML models"]}], "education": [{"degree": "MS", "institution": "University"}]}
    tower = build_candidate_tower_from_resume_profile(profile)
    assert tower.resume_id == "res_123"
    assert tower.full_name == "Jane Candidate"
    assert "Python" in tower.candidate_embedding_text
    assert "Acme" in tower.candidate_embedding_text
