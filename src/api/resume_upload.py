from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status

from ..control.postgres import postgres_session
from ..control.repository import ControlRepository
from ..infrastructure.mongo import get_mongo_database
from ..resume_ocr.logger import build_session_logger
from ..resume_ocr.pipeline import ResumeOcrPipeline, _new_run_session_id
from ..resume_ocr.settings import load_system_config
from ..resume_ocr.utils import SUPPORTED_EXTENSIONS
from ..warehouse.repositories import WarehouseRepository


DEFAULT_MAX_UPLOAD_MB = 15
CHUNK_SIZE_BYTES = 1024 * 1024


def _project_root() -> Path:
    return Path(os.getenv("JOB_MINER_ROOT", ".")).resolve()


def _normalize_email(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _safe_filename(filename: str | None) -> str:
    raw = Path(filename or "resume").name.strip() or "resume"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    return safe[:140] or "resume"


def _max_upload_bytes() -> int:
    raw = os.getenv("JOB_MINER_RESUME_UPLOAD_MAX_MB", str(DEFAULT_MAX_UPLOAD_MB))
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_MAX_UPLOAD_MB
    return max(value, 1) * 1024 * 1024


def _write_upload_to_disk(upload: UploadFile, target_path: Path) -> int:
    max_bytes = _max_upload_bytes()
    total = 0
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        upload.file.seek(0)
        with target_path.open("wb") as out_file:
            while True:
                chunk = upload.file.read(CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Resume file is too large. Maximum allowed size is {max_bytes // (1024 * 1024)} MB.",
                    )
                out_file.write(chunk)
    except HTTPException:
        try:
            target_path.unlink(missing_ok=True)
        finally:
            raise

    if total <= 0:
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded resume file is empty.")

    return total


def _model_to_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"Unsupported resume pipeline payload type: {type(value)!r}")


def process_candidate_resume_upload(
    *,
    upload: UploadFile,
    user: dict[str, Any],
    current_link: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist, parse, warehouse, and link a candidate resume upload.

    The endpoint is intentionally strict: a candidate cannot skip resume upload
    when their profile is incomplete, and a parsed resume email cannot conflict
    with the verified login email. This prevents a user from linking another
    candidate's profile to their account by uploading a different person's CV.
    """
    if not user.get("id"):
        raise HTTPException(status_code=403, detail="Authenticated app user is missing.")

    login_email = _normalize_email(user.get("email"))
    if not login_email or not user.get("email_verified"):
        raise HTTPException(status_code=403, detail="A verified email is required before uploading a resume.")

    original_name = _safe_filename(upload.filename)
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Unsupported resume file type '{suffix}'. Allowed types: {allowed}.")

    root = _project_root()
    upload_id = _new_run_session_id()
    upload_dir = root / "data" / "resumes" / "candidate_uploads" / str(user["id"])
    local_path = upload_dir / f"{upload_id}_{original_name}"
    file_size_bytes = _write_upload_to_disk(upload, local_path)

    config = load_system_config(root)
    run_session_id = _new_run_session_id()
    logger = build_session_logger(root, config.output.log_dir, run_session_id)
    logger.log(
        "candidate_resume_upload_start",
        app_user_id=str(user.get("id")),
        email=login_email,
        source_path=str(local_path),
        file_size_bytes=file_size_bytes,
    )

    pipeline = ResumeOcrPipeline(root, config, logger)
    profile, record, status_text = pipeline.process_file(local_path)
    if status_text != "processed" or profile is None or record is None:
        raise HTTPException(
            status_code=422,
            detail="Resume could not be processed. Check that the file is readable and that the OCR/LLM services are running.",
        )

    profile_payload = _model_to_payload(profile)
    contact = profile_payload.setdefault("contact", {})
    parsed_email = _normalize_email(contact.get("email"))
    if parsed_email and parsed_email != login_email:
        raise HTTPException(
            status_code=409,
            detail="The email extracted from the resume does not match your verified login email. Upload a resume with the same email or contact support.",
        )

    # Keep ownership deterministic. If the resume has no email, the verified
    # login email becomes the canonical candidate email.
    contact["email"] = login_email
    if not contact.get("full_name") and user.get("full_name"):
        contact["full_name"] = user.get("full_name")
    profile_payload["status"] = "ready"
    profile_payload["profile_state"] = "ready"
    profile_payload["onboarding_required"] = False
    profile_payload["resume_uploaded"] = True
    profile_payload.setdefault("raw_payload", {})
    if isinstance(profile_payload["raw_payload"], dict):
        profile_payload["raw_payload"].update(
            {
                "uploaded_by_app_user_id": str(user.get("id")),
                "uploaded_by_email": login_email,
                "upload_source": "candidate_portal",
            }
        )

    record_payload = _model_to_payload(record)
    record_payload["email"] = login_email
    if not record_payload.get("full_name") and contact.get("full_name"):
        record_payload["full_name"] = contact.get("full_name")
    record_payload["status"] = "ready"
    record_payload["profile_state"] = "ready"
    record_payload["onboarding_required"] = False
    record_payload["resume_uploaded"] = True
    record_payload["uploaded_by_app_user_id"] = str(user.get("id"))
    record_payload["uploaded_by_email"] = login_email
    record_payload["recommendation_status"] = "pending"
    record_payload["recommendation_status_message"] = "Profile parsed. Recommendation generation is pending."

    db = get_mongo_database()
    warehouse = WarehouseRepository(db)
    resume_id, _, _ = warehouse.upsert_resume_profile(profile_payload, run_session_id=run_session_id)
    candidate_id = warehouse.upsert_candidate_tower(record_payload)

    # Persist the original uploaded resume for audit/debugging. The parsed OCR
    # outputs are already written by the resume OCR pipeline under data/processed.
    metadata = {
        "link_source": "candidate_resume_upload",
        "uploaded_file_name": original_name,
        "uploaded_file_size_bytes": file_size_bytes,
        "run_session_id": run_session_id,
        "previous_candidate_id": current_link.get("candidate_id") if current_link else None,
        "previous_resume_id": current_link.get("resume_id") if current_link else None,
    }

    with postgres_session() as session:
        repo = ControlRepository(session)
        link = repo.link_candidate(str(user["id"]), candidate_id, resume_id, metadata=metadata)
        repo.add_audit_event(
            organization_id=str(user.get("organization_id")) if user.get("organization_id") else None,
            actor_app_user_id=str(user.get("id")),
            actor_keycloak_user_id=str(user.get("login_keycloak_user_id") or user.get("keycloak_user_id") or ""),
            event_type="candidate.resume_uploaded",
            entity_type="candidate_user_link",
            entity_id=str(link["id"]),
            after_payload={"candidate_id": candidate_id, "resume_id": resume_id, **metadata},
        )

    logger.log(
        "candidate_resume_upload_complete",
        app_user_id=str(user.get("id")),
        candidate_id=candidate_id,
        resume_id=resume_id,
        source_path=str(local_path),
    )

    return {
        "uploaded": True,
        "candidate_id": candidate_id,
        "resume_id": resume_id,
        "candidate_link": link,
        "run_session_id": run_session_id,
        "profile": {
            "resume_profile": profile_payload,
            "candidate_tower": record_payload,
        },
    }
