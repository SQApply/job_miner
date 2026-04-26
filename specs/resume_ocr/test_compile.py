from pathlib import Path

from src.settings import load_system_config
from src.schemas import ResumeProfile
from src.prepare.candidate_tower import build_candidate_tower_record


def test_config_loads():
    root = Path(__file__).resolve().parents[1]
    config = load_system_config(root)
    assert config.ocr.backend in {"glmocr_sdk", "ollama_image"}


def test_candidate_tower_record_minimal():
    profile = ResumeProfile(resume_id="res_1", source_file_name="a.pdf", sha256="abc")
    record = build_candidate_tower_record(profile)
    assert record.candidate_id.startswith("cand_")
    assert record.resume_id == "res_1"
