from __future__ import annotations

from typing import Any

from ..control.repository import ControlRepository


class ApplicationNotificationService:
    def __init__(self, repo: ControlRepository):
        self.repo = repo

    def create_batch_summary(self, *, batch: dict[str, Any], job_runs: list[dict[str, Any]]) -> dict[str, Any]:
        success = int(batch.get("success_count") or 0)
        failed = int(batch.get("failed_count") or 0)
        needs_review = int(batch.get("needs_review_count") or 0)
        skipped = int(batch.get("skipped_count") or 0)
        total = int(batch.get("requested_job_count") or len(job_runs) or 0)
        title = "Application agent summary is ready"
        body = (
            f"{success} of {total} jobs were submitted. "
            f"{failed} failed, {needs_review} need review, and {skipped} were skipped."
        )
        payload = {
            "batch_id": str(batch.get("id")),
            "batch_status": batch.get("batch_status"),
            "requested_job_count": total,
            "success_count": success,
            "failed_count": failed,
            "needs_review_count": needs_review,
            "skipped_count": skipped,
            "email_placeholder": True,
            "jobs": [
                {
                    "job_run_id": str(row.get("id")),
                    "job_id": row.get("job_id"),
                    "run_status": row.get("run_status"),
                    "error_type": row.get("error_type"),
                    "error_message": row.get("error_message"),
                }
                for row in job_runs
            ],
        }
        return self.repo.create_candidate_notification(
            app_user_id=str(batch["app_user_id"]),
            candidate_id=str(batch["candidate_id"]),
            notification_type="application_batch_summary",
            title=title,
            body=body,
            payload=payload,
            channel="candidate_portal",
            status="visible",
        )
