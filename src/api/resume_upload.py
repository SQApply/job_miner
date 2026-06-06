from __future__ import annotations

import os
import re
from datetime import datetime, timezone
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
from ..tasks.recommendation_tasks import generate_recommendations_after_resume_upload_task
from ..tasks.tracking import create_task_tracking_row, mark_task_failed
from ..observability.correlation import new_uuid
from ..warehouse.repositories import WarehouseRepository
from ..common.constants import MongoCollections


DEFAULT_MAX_UPLOAD_MB = 15
CHUNK_SIZE_BYTES = 1024 * 1024

RESUME_PROCESSING_STALE_SECONDS = 60 * 60
PROCESSING_STATES = {"queued", "processing", "profile_extracting"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resume_processing_stale_seconds() -> int:
    raw = os.getenv("JOB_MINER_RESUME_PROCESSING_STALE_SECONDS", str(RESUME_PROCESSING_STALE_SECONDS))
    try:
        return max(int(raw), 60)
    except ValueError:
        return RESUME_PROCESSING_STALE_SECONDS


def _is_stale_timestamp(value: Any) -> bool:
    if not value:
        return True

    if isinstance(value, datetime):
        started_at = value
    else:
        try:
            started_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return True

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    elapsed = (_utc_now() - started_at).total_seconds()
    return elapsed > _resume_processing_stale_seconds()


def _assert_no_active_resume_processing(db: Any, candidate_id: str | None) -> None:
    if not candidate_id:
        return

    tower = db[MongoCollections.CANDIDATE_TOWER_RECORDS].find_one(
        {"candidate_id": candidate_id},
        {
            "_id": 0,
            "profile_state": 1,
            "resume_upload_status": 1,
            "resume_processing_started_at": 1,
            "resume_upload_started_at": 1,
            "active_resume_upload_id": 1,
        },
    )

    if not tower:
        return

    status_values = {
        str(tower.get("resume_upload_status") or "").lower(),
        str(tower.get("profile_state") or "").lower(),
    }

    is_processing = bool(status_values.intersection(PROCESSING_STATES))
    started_at = tower.get("resume_processing_started_at") or tower.get("resume_upload_started_at")

    if is_processing and not _is_stale_timestamp(started_at):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Your resume is already being processed. Refreshing or uploading again is not required. "
                "Please wait for the current processing run to finish."
            ),
        )


def _mark_resume_processing_started(
    *,
    db: Any,
    candidate_id: str | None,
    resume_id: str | None,
    upload_id: str,
    original_name: str,
    user: dict[str, Any],
    local_path: Path,
    status_value: str = "processing",
    status_message: str = "Your resume is being processed. Please do not upload it again.",
    extra: dict[str, Any] | None = None,
) -> None:
    if not candidate_id:
        return

    now = _utc_now()

    payload = {
        "profile_state": "processing",
        "resume_upload_status": status_value,
        "resume_upload_status_message": status_message,
        "resume_processing_started_at": now,
        "resume_upload_started_at": now,
        "resume_upload_updated_at": now,
        "active_resume_upload_id": upload_id,
        "active_resume_file_name": original_name,
        "active_resume_local_path": str(local_path),
        "uploaded_by_app_user_id": str(user.get("id")),
        "uploaded_by_email": _normalize_email(user.get("email")),
        "onboarding_required": True,
    }
    if status_value == "queued":
        payload["resume_upload_queued_at"] = now
    if extra:
        payload.update(extra)

    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
        {"candidate_id": candidate_id},
        {"$set": payload},
    )

    if resume_id:
        db[MongoCollections.RESUME_PROFILES_CURRENT].update_one(
            {"resume_id": resume_id},
            {"$set": payload},
        )


def _mark_resume_processing_failed(
    *,
    db: Any,
    candidate_id: str | None,
    resume_id: str | None,
    upload_id: str,
    error_message: str,
) -> None:
    if not candidate_id:
        return

    now = _utc_now()

    payload = {
        "profile_state": "incomplete",
        "resume_upload_status": "failed",
        "resume_upload_status_message": "Resume processing failed. Please check the file and upload again.",
        "resume_upload_error": error_message[:4000],
        "resume_upload_failed_at": now,
        "resume_upload_updated_at": now,
        "active_resume_upload_id": upload_id,
        "onboarding_required": True,
    }

    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
        {"candidate_id": candidate_id},
        {"$set": payload},
    )

    if resume_id:
        db[MongoCollections.RESUME_PROFILES_CURRENT].update_one(
            {"resume_id": resume_id},
            {"$set": payload},
        )


def _mark_previous_shell_superseded(
    *,
    db: Any,
    previous_candidate_id: str | None,
    previous_resume_id: str | None,
    new_candidate_id: str,
    new_resume_id: str,
    upload_id: str,
) -> None:
    if not previous_candidate_id or previous_candidate_id == new_candidate_id:
        return

    now = _utc_now()

    payload = {
        "profile_state": "superseded",
        "resume_upload_status": "completed",
        "resume_upload_status_message": "Resume processing completed and a full candidate profile was created.",
        "resume_upload_completed_at": now,
        "resume_upload_updated_at": now,
        "active_resume_upload_id": upload_id,
        "superseded_by_candidate_id": new_candidate_id,
        "superseded_by_resume_id": new_resume_id,
        "onboarding_required": False,
    }

    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
        {"candidate_id": previous_candidate_id},
        {"$set": payload},
    )

    if previous_resume_id:
        db[MongoCollections.RESUME_PROFILES_CURRENT].update_one(
            {"resume_id": previous_resume_id},
            {"$set": payload},
        )


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


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resume_upload_mode() -> str:
    raw = os.getenv("JOB_MINER_RESUME_UPLOAD_MODE", "sync").strip().lower()
    return "async" if raw in {"async", "queued", "background", "celery"} else "sync"


def _resume_processing_queue_name() -> str:
    return os.getenv("JOB_MINER_RESUME_PROCESSING_QUEUE", "resume_processing_queue")


def _resume_upload_user_context(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(user.get("id")) if user.get("id") is not None else None,
        "organization_id": str(user.get("organization_id")) if user.get("organization_id") is not None else None,
        "email": user.get("email"),
        "email_verified": bool(user.get("email_verified")),
        "full_name": user.get("full_name"),
        "keycloak_user_id": user.get("keycloak_user_id"),
        "login_keycloak_user_id": user.get("login_keycloak_user_id"),
        "preferred_username": user.get("preferred_username"),
    }


def _resume_upload_link_context(current_link: dict[str, Any] | None) -> dict[str, Any] | None:
    if not current_link:
        return None
    return {
        "candidate_id": str(current_link.get("candidate_id")) if current_link.get("candidate_id") else None,
        "resume_id": str(current_link.get("resume_id")) if current_link.get("resume_id") else None,
    }


def _mark_resume_processing_extracting(
    *,
    db: Any,
    candidate_id: str | None,
    resume_id: str | None,
    upload_id: str,
) -> None:
    if not candidate_id:
        return

    now = _utc_now()
    payload = {
        "profile_state": "processing",
        "resume_upload_status": "profile_extracting",
        "resume_upload_status_message": "Your resume is being parsed and converted into a candidate profile.",
        "resume_processing_started_at": now,
        "resume_upload_updated_at": now,
        "active_resume_upload_id": upload_id,
        "onboarding_required": True,
    }
    db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one({"candidate_id": candidate_id}, {"$set": payload})
    if resume_id:
        db[MongoCollections.RESUME_PROFILES_CURRENT].update_one({"resume_id": resume_id}, {"$set": payload})


def _queue_recommendation_generation(
    *,
    db: Any,
    candidate_id: str,
    resume_id: str,
    login_email: str,
    run_session_id: str,
    request_id: str | None,
    user: dict[str, Any] | None,
) -> dict[str, Any]:
    """Queue recommendation generation without blocking resume upload.

    If Redis/Celery is unavailable, the upload still succeeds and the candidate
    record is marked with a queue failure that can be retried operationally.
    """
    now = datetime.now(timezone.utc)

    if not _env_enabled("JOB_MINER_ENABLE_AUTO_RECOMMENDATION_QUEUE", True):
        db.candidate_tower_records.update_many(
            {"candidate_id": candidate_id},
            {
                "$set": {
                    "recommendation_status": "disabled",
                    "recommendation_status_message": "Automatic recommendation generation is disabled.",
                    "recommendation_updated_at": now,
                }
            },
        )
        return {
            "status": "disabled",
            "candidate_id": candidate_id,
            "message": "Automatic recommendation generation is disabled.",
        }

    queue_name = os.getenv("JOB_MINER_RECOMMENDATION_QUEUE", "recommendation_queue")
    task_uuid = new_uuid()
    task_payload = {
        "request_id": request_id,
        "candidate_id": candidate_id,
        "resume_id": resume_id,
        "email": login_email,
        "run_session_id": run_session_id,
        "source": "candidate_resume_upload_celery",
    }
    create_task_tracking_row(
        task_uuid=task_uuid,
        task_name="src.tasks.recommendation_tasks.generate_recommendations_after_resume_upload_task",
        queue_name=queue_name,
        user=user,
        payload=task_payload,
    )

    try:
        async_result = generate_recommendations_after_resume_upload_task.apply_async(
            kwargs={
                "candidate_id": candidate_id,
                "resume_id": resume_id,
                "email": login_email,
                "run_session_id": run_session_id,
                "request_id": request_id,
            },
            queue=queue_name,
            task_id=task_uuid,
        )
    except Exception as exc:
        mark_task_failed(
            task_uuid=task_uuid,
            error=exc,
            entity_type="recommendation_generation",
            entity_id=candidate_id,
            failed_payload=task_payload,
            message="Recommendation task could not be queued.",
        )
        db.candidate_tower_records.update_many(
            {"candidate_id": candidate_id},
            {
                "$set": {
                    "recommendation_status": "queue_failed",
                    "recommendation_status_message": "Profile is ready, but recommendation generation could not be queued.",
                    "recommendation_error": repr(exc),
                    "recommendation_updated_at": now,
                }
            },
        )
        return {
            "status": "queue_failed",
            "candidate_id": candidate_id,
            "message": "Recommendation generation could not be queued.",
            "error": repr(exc),
        }

    db.candidate_tower_records.update_many(
        {"candidate_id": candidate_id},
        {
            "$set": {
                "recommendation_status": "queued",
                "recommendation_status_message": "Recommendations are being generated in the background.",
                "recommendation_task_id": task_uuid,
                "recommendation_queue": queue_name,
                "recommendation_queued_at": now,
                "recommendation_updated_at": now,
                "recommendation_error": None,
            }
        },
    )

    return {
        "status": "queued",
        "candidate_id": candidate_id,
        "task_id": task_uuid,
        "queue": queue_name,
        "message": "Recommendations are being generated in the background.",
    }


def process_candidate_resume_upload(
    *,
    upload: UploadFile,
    user: dict[str, Any],
    current_link: dict[str, Any] | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Validate and persist a candidate resume upload.

    Default `sync` mode keeps the original behavior for local development and existing tests.
    Production can set `JOB_MINER_RESUME_UPLOAD_MODE=async` to return 202 from FastAPI
    after storing the file and queueing the heavy OCR/profile extraction in Celery.
    """
    if not user.get("id"):
        raise HTTPException(status_code=403, detail="Authenticated app user is missing.")

    login_email = _normalize_email(user.get("email"))
    if not login_email or not user.get("email_verified"):
        raise HTTPException(status_code=403, detail="A verified email is required before uploading a resume.")

    request_id = request_id or new_uuid()

    original_name = _safe_filename(upload.filename)
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Unsupported resume file type '{suffix}'. Allowed types: {allowed}.")

    root = _project_root()
    upload_id = _new_run_session_id()
    upload_dir = root / "data" / "resumes" / "candidate_uploads" / str(user["id"])
    local_path = upload_dir / f"{upload_id}_{original_name}"

    previous_candidate_id = (
        str(current_link.get("candidate_id"))
        if current_link and current_link.get("candidate_id")
        else None
    )
    previous_resume_id = (
        str(current_link.get("resume_id"))
        if current_link and current_link.get("resume_id")
        else None
    )

    db = get_mongo_database()
    _assert_no_active_resume_processing(db, previous_candidate_id)

    async_mode = _resume_upload_mode() == "async"
    started_status = "queued" if async_mode else "processing"
    started_message = (
        "Your resume upload was accepted and queued for background processing."
        if async_mode
        else "Your resume is being processed. Please do not upload it again."
    )
    _mark_resume_processing_started(
        db=db,
        candidate_id=previous_candidate_id,
        resume_id=previous_resume_id,
        upload_id=upload_id,
        original_name=original_name,
        user=user,
        local_path=local_path,
        status_value=started_status,
        status_message=started_message,
        extra={"resume_upload_request_id": request_id},
    )

    try:
        file_size_bytes = _write_upload_to_disk(upload, local_path)
    except HTTPException as exc:
        _mark_resume_processing_failed(
            db=db,
            candidate_id=previous_candidate_id,
            resume_id=previous_resume_id,
            upload_id=upload_id,
            error_message=str(exc.detail),
        )
        raise

    if async_mode:
        queue_name = _resume_processing_queue_name()
        task_uuid = new_uuid()
        task_payload = {
            "request_id": request_id,
            "local_path": str(local_path),
            "original_name": original_name,
            "file_size_bytes": file_size_bytes,
            "upload_id": upload_id,
            "candidate_id": previous_candidate_id,
            "resume_id": previous_resume_id,
        }
        create_task_tracking_row(
            task_uuid=task_uuid,
            task_name="src.tasks.resume_tasks.process_candidate_resume_upload_task",
            queue_name=queue_name,
            user=user,
            payload=task_payload,
        )

        try:
            from ..tasks.resume_tasks import process_candidate_resume_upload_task

            async_result = process_candidate_resume_upload_task.apply_async(
                kwargs={
                    "local_path": str(local_path),
                    "original_name": original_name,
                    "file_size_bytes": file_size_bytes,
                    "upload_id": upload_id,
                    "user": _resume_upload_user_context(user),
                    "current_link": _resume_upload_link_context(current_link),
                    "request_id": request_id,
                },
                queue=queue_name,
                task_id=task_uuid,
            )
        except Exception as exc:
            mark_task_failed(
                task_uuid=task_uuid,
                error=exc,
                entity_type="resume_upload",
                entity_id=previous_candidate_id or upload_id,
                failed_payload=task_payload,
                message="Resume processing task could not be queued.",
            )
            _mark_resume_processing_failed(
                db=db,
                candidate_id=previous_candidate_id,
                resume_id=previous_resume_id,
                upload_id=upload_id,
                error_message=f"Resume processing could not be queued: {exc!r}",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Resume upload was saved, but background processing could not be queued. Please try again.",
            ) from exc

        now = _utc_now()
        queue_payload = {
            "resume_processing_task_id": task_uuid,
            "resume_upload_request_id": request_id,
            "resume_processing_queue": queue_name,
            "resume_processing_queued_at": now,
            "resume_upload_updated_at": now,
        }
        if previous_candidate_id:
            db[MongoCollections.CANDIDATE_TOWER_RECORDS].update_one(
                {"candidate_id": previous_candidate_id},
                {"$set": queue_payload},
            )
        if previous_resume_id:
            db[MongoCollections.RESUME_PROFILES_CURRENT].update_one(
                {"resume_id": previous_resume_id},
                {"$set": queue_payload},
            )

        return {
            "uploaded": True,
            "queued": True,
            "candidate_id": previous_candidate_id,
            "resume_id": previous_resume_id,
            "candidate_link": current_link,
            "upload_id": upload_id,
            "original_file_name": original_name,
            "uploaded_file_size_bytes": file_size_bytes,
            "local_path": str(local_path),
            "resume_processing": {
                "status": "queued",
                "task_id": task_uuid,
                "queue": queue_name,
                "message": "Resume processing is running in the background.",
            },
            "recommendation_generation": {
                "status": "waiting_for_resume_processing",
                "message": "Recommendations will be queued after the resume profile is parsed.",
            },
        }

    return _process_candidate_resume_file_from_path(
        local_path=local_path,
        original_name=original_name,
        file_size_bytes=file_size_bytes,
        upload_id=upload_id,
        user=_resume_upload_user_context(user),
        current_link=_resume_upload_link_context(current_link),
        request_id=request_id,
    )


def _process_candidate_resume_file_from_path(
    *,
    local_path: str | Path,
    original_name: str,
    file_size_bytes: int,
    upload_id: str,
    user: dict[str, Any],
    current_link: dict[str, Any] | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Run the heavy OCR/profile extraction step for a previously stored upload.

    This function is shared by the original synchronous upload path and the new
    Celery-backed 202 path. Keeping the heavy logic in one place prevents the sync
    and async paths from drifting apart.
    """
    if not user.get("id"):
        raise HTTPException(status_code=403, detail="Authenticated app user is missing.")

    login_email = _normalize_email(user.get("email"))
    if not login_email or not user.get("email_verified"):
        raise HTTPException(status_code=403, detail="A verified email is required before uploading a resume.")

    request_id = request_id or new_uuid()

    local_path = Path(local_path)
    previous_candidate_id = (
        str(current_link.get("candidate_id"))
        if current_link and current_link.get("candidate_id")
        else None
    )
    previous_resume_id = (
        str(current_link.get("resume_id"))
        if current_link and current_link.get("resume_id")
        else None
    )

    db = get_mongo_database()
    _mark_resume_processing_extracting(
        db=db,
        candidate_id=previous_candidate_id,
        resume_id=previous_resume_id,
        upload_id=upload_id,
    )

    root = _project_root()
    config = load_system_config(root)
    run_session_id = _new_run_session_id()
    logger = build_session_logger(root, config.output.log_dir, run_session_id)
    logger.log(
        "candidate_resume_upload_start",
        app_user_id=str(user.get("id")),
        email=login_email,
        source_path=str(local_path),
        file_size_bytes=file_size_bytes,
        async_mode=_resume_upload_mode() == "async",
    )

    pipeline = ResumeOcrPipeline(root, config, logger)

    try:
        profile, record, status_text = pipeline.process_file(local_path)

        if status_text != "processed" or profile is None or record is None:
            raise HTTPException(
                status_code=422,
                detail="Resume could not be processed. Check that the file is readable and that the OCR/LLM services are running.",
            )

    except HTTPException as exc:
        _mark_resume_processing_failed(
            db=db,
            candidate_id=previous_candidate_id,
            resume_id=previous_resume_id,
            upload_id=upload_id,
            error_message=str(exc.detail),
        )
        raise

    except Exception as exc:
        _mark_resume_processing_failed(
            db=db,
            candidate_id=previous_candidate_id,
            resume_id=previous_resume_id,
            upload_id=upload_id,
            error_message=repr(exc),
        )
        raise

    profile_payload = _model_to_payload(profile)
    contact = profile_payload.setdefault("contact", {})
    parsed_email = _normalize_email(contact.get("email"))
    if parsed_email and parsed_email != login_email:
        _mark_resume_processing_failed(
            db=db,
            candidate_id=previous_candidate_id,
            resume_id=previous_resume_id,
            upload_id=upload_id,
            error_message="The email extracted from the resume does not match the verified login email.",
        )
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
    profile_payload["resume_upload_status"] = "completed"
    profile_payload["resume_upload_status_message"] = "Resume processing completed."
    profile_payload["resume_upload_completed_at"] = _utc_now()
    profile_payload["resume_upload_updated_at"] = _utc_now()
    profile_payload["active_resume_upload_id"] = upload_id
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
    record_payload["resume_upload_status"] = "completed"
    record_payload["resume_upload_status_message"] = "Resume processing completed."
    record_payload["resume_upload_completed_at"] = _utc_now()
    record_payload["resume_upload_updated_at"] = _utc_now()
    record_payload["active_resume_upload_id"] = upload_id
    record_payload["uploaded_by_app_user_id"] = str(user.get("id"))
    record_payload["uploaded_by_email"] = login_email
    record_payload["recommendation_status"] = "pending"
    record_payload["recommendation_status_message"] = "Profile parsed. Recommendation generation is pending."

    warehouse = WarehouseRepository(db)
    resume_id, _, _ = warehouse.upsert_resume_profile(profile_payload, run_session_id=run_session_id)
    candidate_id = warehouse.upsert_candidate_tower(record_payload)

    _mark_previous_shell_superseded(
        db=db,
        previous_candidate_id=previous_candidate_id,
        previous_resume_id=previous_resume_id,
        new_candidate_id=candidate_id,
        new_resume_id=resume_id,
        upload_id=upload_id,
    )

    metadata = {
        "link_source": "candidate_resume_upload",
        "uploaded_file_name": original_name,
        "uploaded_file_size_bytes": file_size_bytes,
        "run_session_id": run_session_id,
        "previous_candidate_id": previous_candidate_id,
        "previous_resume_id": previous_resume_id,
        "async_resume_upload": _resume_upload_mode() == "async",
        "request_id": request_id,
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

    recommendation_generation = _queue_recommendation_generation(
        db=db,
        candidate_id=candidate_id,
        resume_id=resume_id,
        login_email=login_email,
        run_session_id=run_session_id,
        request_id=request_id,
        user=user,
    )

    logger.log(
        "candidate_recommendation_generation_queued",
        app_user_id=str(user.get("id")),
        candidate_id=candidate_id,
        resume_id=resume_id,
        recommendation_generation=recommendation_generation,
    )

    return {
        "uploaded": True,
        "queued": False,
        "candidate_id": candidate_id,
        "resume_id": resume_id,
        "candidate_link": link,
        "run_session_id": run_session_id,
        "recommendation_generation": recommendation_generation,
        "profile": {
            "resume_profile": profile_payload,
            "candidate_tower": record_payload,
        },
    }
