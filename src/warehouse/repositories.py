from __future__ import annotations

from typing import Any, Iterable

from pymongo import UpdateOne
from pymongo.collection import Collection
from pymongo.database import Database

from .base import utc_now
from .documents import (
    CandidateJobMatchDocument,
    CandidateTowerDocument,
    JobCurrentDocument,
    JobHistoryDocument,
    JobRawExtractionDocument,
    JobTowerDocument,
    QdrantIndexStateDocument,
    ResumeProfileCurrentDocument,
    ResumeProfileHistoryDocument,
    WarehouseRunSessionDocument,
)
from .hashing import make_candidate_id, make_job_id, make_resume_id_from_sha, stable_hash
from .url_utils import canonical_job_url, canonical_job_urls
from .job_dates import parse_job_posted_at
from .serializers import as_str_list, to_plain_data
from .skill_utils import build_canonical_candidate_skills, unique_skills


def _split_mongo_update_payload(doc: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a MongoDocument into safe $set and $setOnInsert payloads.

    MongoDB does not allow the same field to appear in both $set and $setOnInsert.
    Also, _id must never be updated after insert.
    """
    data = doc.to_mongo()

    document_id = data.pop("_id", None)
    created_at = data.pop("created_at", getattr(doc, "created_at", utc_now()))

    set_on_insert: dict[str, Any] = {
        "created_at": created_at,
    }

    if document_id is not None:
        set_on_insert["_id"] = document_id

    return data, set_on_insert


class WarehouseRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def canonical_job_url(value: str | None) -> str:
        return canonical_job_url(value)

    @staticmethod
    def _job_url_candidates(value: str | None) -> list[str]:
        raw = str(value or "").strip()
        canonical = canonical_job_url(raw)
        return [url for url in dict.fromkeys([raw, canonical]) if url]

    @staticmethod
    def _job_url_filter(target_id: str, urls: list[str]) -> dict[str, Any]:
        candidates: list[str] = []
        for url in urls:
            candidates.extend(WarehouseRepository._job_url_candidates(url))
        values = list(dict.fromkeys(candidates))
        if not values:
            return {"target_id": target_id, "_id": {"$exists": False}}
        return {
            "target_id": target_id,
            "$or": [
                {"job_url": {"$in": values}},
                {"source_url": {"$in": values}},
                {"apply_url": {"$in": values}},
                {"canonical_job_url": {"$in": values}},
            ],
        }

    def _jobs_current(self) -> Collection:
        return self.db[JobCurrentDocument.collection_name]

    def _find_existing_job_for_payload(self, *, target_id: str, payload: dict[str, Any], computed_job_id: str) -> dict[str, Any] | None:
        source_url = payload.get("source_url") or payload.get("job_url") or payload.get("url") or payload.get("apply_url")
        url_candidates = self._job_url_candidates(str(source_url or ""))
        filters: list[dict[str, Any]] = [{"job_id": computed_job_id}]
        if url_candidates:
            filters.append(self._job_url_filter(target_id, url_candidates))
        job_reference = str(payload.get("job_reference") or payload.get("reference") or "").strip()
        if job_reference:
            filters.append({"target_id": target_id, "job_reference": job_reference})

        for query in filters:
            existing = self._jobs_current().find_one(query)
            if existing:
                return existing
        return None

    def plan_detail_rescrape(
        self,
        *,
        target_id: str,
        run_session_id: str,
        discovered_urls: list[str],
        force_detail_refresh: bool = False,
        deep_refresh_days: int = 14,
    ) -> dict[str, Any]:
        """Decide which discovered URLs need expensive detail-page extraction.

        Discovery runs are cheap and should happen every cycle. Detail extraction
        is expensive because it opens a detail page and may call the local LLM.
        This planner marks known jobs as seen immediately and returns only URLs
        that are new, inactive/reactivated, incomplete, previously failed, or due
        for a periodic deep refresh.
        """
        from datetime import datetime, timedelta

        from .job_dates import ensure_utc

        now = utc_now()
        normalized_urls = canonical_job_urls(discovered_urls)
        if not normalized_urls:
            return {
                "target_id": target_id,
                "run_session_id": run_session_id,
                "discovered_urls": 0,
                "urls_to_extract": [],
                "known_skipped": 0,
                "new_urls": 0,
                "due_for_refresh": 0,
                "reactivated_or_incomplete": 0,
                "force_detail_refresh": bool(force_detail_refresh),
            }

        # Read target docs and compare canonicalized URL keys so older records
        # saved with tracking parameters still match the new discovery URL.
        existing_docs = list(self._jobs_current().find({"target_id": target_id}))
        by_url: dict[str, dict[str, Any]] = {}
        for doc in existing_docs:
            for field in ("canonical_job_url", "job_url", "source_url", "apply_url"):
                key = canonical_job_url(doc.get(field))
                if key:
                    by_url[key] = doc

        refresh_cutoff = now - timedelta(days=max(1, int(deep_refresh_days)))
        urls_to_extract: list[str] = []
        known_skipped = 0
        new_urls = 0
        due_for_refresh = 0
        reactivated_or_incomplete = 0
        seen_existing_ids: list[str] = []

        for url in normalized_urls:
            existing = by_url.get(url)
            if not existing:
                new_urls += 1
                urls_to_extract.append(url)
                continue

            job_id = str(existing.get("job_id") or existing.get("_id") or "").strip()
            if job_id:
                seen_existing_ids.append(job_id)

            inactive = existing.get("is_active") is False
            failed_or_missing = str(existing.get("freshness_status") or "").lower() in {
                "detail_extract_failed",
                "parse_failed",
                "validation_failed",
                "inactive",
                "missing_in_latest_scrape",
            }
            incomplete = not str(existing.get("title") or "").strip() or not str(existing.get("company") or "").strip()
            last_deep = existing.get("last_deep_scraped_at") or existing.get("last_seen_at")
            last_deep_utc = (
                ensure_utc(last_deep)
                if isinstance(last_deep, datetime)
                else None
            )
            due = bool(last_deep_utc and last_deep_utc < refresh_cutoff)

            if force_detail_refresh or inactive or failed_or_missing or incomplete or due:
                urls_to_extract.append(url)
                if due:
                    due_for_refresh += 1
                if inactive or failed_or_missing or incomplete:
                    reactivated_or_incomplete += 1
            else:
                known_skipped += 1

        if seen_existing_ids:
            self._jobs_current().update_many(
                {"job_id": {"$in": sorted(set(seen_existing_ids))}},
                {
                    "$set": {
                        "last_seen_at": now,
                        "last_run_session_id": run_session_id,
                        "freshness_status": "active",
                        "missing_count": 0,
                        "missing_complete_run_count": 0,
                        "is_active": True,
                        "updated_at": now,
                    },
                    "$unset": {
                        "inactive_reason": "",
                        "deactivated_at": "",
                        "deactivation_run_id": "",
                        "missing_since": "",
                    },
                },
            )

        return {
            "target_id": target_id,
            "run_session_id": run_session_id,
            "discovered_urls": len(normalized_urls),
            "urls_to_extract": urls_to_extract,
            "urls_to_extract_count": len(urls_to_extract),
            "known_skipped": known_skipped,
            "new_urls": new_urls,
            "due_for_refresh": due_for_refresh,
            "reactivated_or_incomplete": reactivated_or_incomplete,
            "force_detail_refresh": bool(force_detail_refresh),
            "deep_refresh_days": int(deep_refresh_days),
        }

    def reconcile_missing_jobs_after_discovery(
        self,
        *,
        target_id: str,
        run_session_id: str,
        discovered_urls: list[str],
        deactivate_after_misses: int = 2,
        min_discovery_coverage_ratio: float = 0.25,
        allow_empty_discovery: bool = False,
    ) -> dict[str, Any]:
        """Mark active jobs missing only after a reliable listing-discovery run.

        A single bad scrape must not deactivate the catalog. The coverage guard
        skips lifecycle reconciliation when a portal suddenly returns a suspicious
        small number of URLs compared with the current active catalog.
        """
        now = utc_now()
        normalized_urls = canonical_job_urls(discovered_urls)
        active_count = self._jobs_current().count_documents({"target_id": target_id, "is_active": True})

        if not normalized_urls and not allow_empty_discovery:
            return {
                "status": "skipped_empty_discovery",
                "active_jobs": active_count,
                "discovered_urls": 0,
                "missing_marked": 0,
                "deactivated": 0,
            }

        if active_count and len(normalized_urls) < max(1, int(active_count * float(min_discovery_coverage_ratio))):
            return {
                "status": "skipped_low_coverage",
                "active_jobs": active_count,
                "discovered_urls": len(normalized_urls),
                "min_discovery_coverage_ratio": min_discovery_coverage_ratio,
                "missing_marked": 0,
                "deactivated": 0,
            }

        normalized_set = set(normalized_urls)
        seen_ids: list[str] = []
        reactivated_ids: list[str] = []
        for doc in self._jobs_current().find(
            {"target_id": target_id},
            {
                "job_id": 1,
                "job_url": 1,
                "source_url": 1,
                "apply_url": 1,
                "canonical_job_url": 1,
                "is_active": 1,
            },
        ):
            doc_keys = {
                canonical_job_url(doc.get("canonical_job_url")),
                canonical_job_url(doc.get("job_url")),
                canonical_job_url(doc.get("source_url")),
                canonical_job_url(doc.get("apply_url")),
            }
            if normalized_set.intersection({key for key in doc_keys if key}):
                job_id = str(doc.get("job_id") or doc.get("_id") or "").strip()
                if job_id:
                    seen_ids.append(job_id)
                    if doc.get("is_active") is False:
                        reactivated_ids.append(job_id)

        if seen_ids:
            self._jobs_current().update_many(
                {"target_id": target_id, "job_id": {"$in": sorted(set(seen_ids))}},
                {
                    "$set": {
                        "last_seen_at": now,
                        "last_run_session_id": run_session_id,
                        "freshness_status": "active",
                        "missing_count": 0,
                        "missing_complete_run_count": 0,
                        "is_active": True,
                        "updated_at": now,
                    },
                    "$unset": {
                        "inactive_reason": "",
                        "deactivated_at": "",
                        "deactivation_run_id": "",
                        "missing_since": "",
                    },
                },
            )
        if reactivated_ids:
            self._jobs_current().update_many(
                {"target_id": target_id, "job_id": {"$in": sorted(set(reactivated_ids))}},
                {"$set": {"reactivated_at": now, "updated_at": now}},
            )

        missing_filter: dict[str, Any] = {"target_id": target_id, "is_active": True}
        if seen_ids:
            missing_filter["job_id"] = {"$nin": sorted(set(seen_ids))}

        missing_documents = list(
            self._jobs_current().find(
                missing_filter,
                {"job_id": 1, "missing_count": 1, "missing_since": 1},
            )
        )
        missing_ids = sorted(
            {
                str(doc.get("job_id") or doc.get("_id") or "").strip()
                for doc in missing_documents
                if str(doc.get("job_id") or doc.get("_id") or "").strip()
            }
        )
        first_missing_ids = sorted(
            {
                str(doc.get("job_id") or doc.get("_id") or "").strip()
                for doc in missing_documents
                if not doc.get("missing_since")
                and str(doc.get("job_id") or doc.get("_id") or "").strip()
            }
        )

        missing_result = self._jobs_current().update_many(
            missing_filter,
            {
                "$inc": {
                    "missing_count": 1,
                    "missing_complete_run_count": 1,
                },
                "$set": {
                    "freshness_status": "missing_in_latest_scrape",
                    "last_missing_at": now,
                    "updated_at": now,
                },
            },
        )
        if first_missing_ids:
            self._jobs_current().update_many(
                {
                    "target_id": target_id,
                    "job_id": {"$in": first_missing_ids},
                },
                {"$set": {"missing_since": now, "updated_at": now}},
            )

        threshold = max(1, int(deactivate_after_misses))
        deactivate_result = self._jobs_current().update_many(
            {
                "target_id": target_id,
                "is_active": True,
                "missing_complete_run_count": {"$gte": threshold},
            },
            {
                "$set": {
                    "is_active": False,
                    "freshness_status": "inactive",
                    "inactive_reason": "not_seen_in_successive_scrapes",
                    "deactivated_at": now,
                    "deactivation_run_id": run_session_id,
                    "updated_at": now,
                }
            },
        )
        deactivation_candidates = list(
            self._jobs_current().find(
                {
                    "target_id": target_id,
                    "is_active": False,
                    "deactivation_run_id": run_session_id,
                },
                {"job_id": 1},
            )
        )
        deactivated_ids = sorted(
            {
                str(doc.get("job_id") or doc.get("_id") or "").strip()
                for doc in deactivation_candidates
                if str(doc.get("job_id") or doc.get("_id") or "").strip()
            }
        )
        return {
            "status": "completed",
            "active_jobs_before_reconcile": active_count,
            "discovered_urls": len(normalized_urls),
            "missing_marked": int(missing_result.modified_count),
            "missing_job_ids": missing_ids,
            "deactivated": int(deactivate_result.modified_count),
            "deactivated_job_ids": deactivated_ids,
            "reactivated": len(reactivated_ids),
            "reactivated_job_ids": sorted(set(reactivated_ids)),
            "deactivate_after_misses": threshold,
        }

    def record_recommendation_refresh_requests(
        self,
        *,
        portal_id: str,
        target_id: str,
        run_session_id: str,
        changed_job_ids: list[str],
        candidate_limit: int = 250,
    ) -> dict[str, Any]:
        """Record a bounded recommendation-refresh backlog instead of immediate fan-out.

        New/changed portal jobs should not trigger recommendation regeneration for
        every candidate at once. This method records a capped set of pending
        refresh requests that a separate worker drains in controlled batches.
        """
        normalized_jobs = sorted({str(job_id) for job_id in changed_job_ids if str(job_id or "").strip()})
        if not normalized_jobs:
            return {"status": "skipped_no_changed_jobs", "requests_created": 0, "changed_job_count": 0}

        now = utc_now()
        candidates = list(self.db["candidate_tower_records"].find(
            {
                "$and": [
                    {"candidate_id": {"$exists": True, "$ne": None}},
                    {"profile_state": {"$ne": "incomplete"}},
                ]
            },
            {"candidate_id": 1, "resume_id": 1, "email": 1, "recommendation_status": 1, "updated_at": 1},
        ).sort("updated_at", -1).limit(max(1, int(candidate_limit))))

        requests = []
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            if not candidate_id:
                continue
            requests.append(UpdateOne(
                {"candidate_id": candidate_id, "status": {"$in": ["pending", "queued", "running"]}},
                {
                    "$setOnInsert": {
                        "request_id": stable_hash(
                            {
                                "kind": "recommendation_refresh_request",
                                "portal_id": portal_id,
                                "run_session_id": run_session_id,
                                "candidate_id": candidate_id,
                            }
                        )[:32],
                        "candidate_id": candidate_id,
                        "resume_id": candidate.get("resume_id"),
                        "email": candidate.get("email"),
                        "created_at": now,
                    },
                    "$set": {
                        "status": "pending",
                        "reason": "portal_jobs_changed",
                        "portal_id": portal_id,
                        "target_id": target_id,
                        "run_session_id": run_session_id,
                        "changed_job_ids": normalized_jobs[:200],
                        "changed_job_count": len(normalized_jobs),
                        "priority": 50,
                        "updated_at": now,
                    },
                },
                upsert=True,
            ))
        if not requests:
            return {"status": "skipped_no_candidates", "requests_created": 0, "changed_job_count": len(normalized_jobs)}
        result = self.db["recommendation_refresh_requests"].bulk_write(requests, ordered=False)
        return {
            "status": "recorded_pending_requests",
            "candidate_scan_limit": int(candidate_limit),
            "changed_job_count": len(normalized_jobs),
            "requests_created": int(result.upserted_count),
            "requests_matched_existing": int(result.matched_count),
        }

    def upsert_run_session(self, doc: WarehouseRunSessionDocument) -> None:
        data, set_on_insert = _split_mongo_update_payload(doc)

        self.db[doc.collection_name].update_one(
            {"run_session_id": doc.run_session_id},
            {
                "$set": data,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

    def upsert_job_raw(self, doc: JobRawExtractionDocument) -> None:
        data, set_on_insert = _split_mongo_update_payload(doc)

        self.db[doc.collection_name].update_one(
            {"raw_id": doc.raw_id},
            {
                "$set": data,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

    def upsert_job(
        self,
        job_payload: dict[str, Any],
        *,
        target_id: str,
        run_session_id: str | None = None,
    ) -> tuple[str, bool, bool]:
        payload = to_plain_data(job_payload)

        computed_job_id = make_job_id(payload, target_id=target_id)
        # Portal freshness strings (for example, "3 days ago") change on every
        # crawl even when the job itself has not changed. Keep them out of the
        # version/history hash while still persisting them on the current record.
        content_hash_payload = dict(payload)
        content_hash_payload.pop("posted_date", None)
        content_hash = stable_hash(content_hash_payload)
        now = utc_now()

        existing = self._find_existing_job_for_payload(
            target_id=target_id,
            payload=payload,
            computed_job_id=computed_job_id,
        )
        job_id = str((existing or {}).get("job_id") or computed_job_id)

        changed = existing is None or existing.get("content_hash") != content_hash

        if changed:
            version = int((existing or {}).get("version") or 0) + 1
        else:
            version = int(existing.get("version") or 1)

        source_url = (
            payload.get("source_url")
            or payload.get("job_url")
            or payload.get("url")
        )
        canonical_source_url = canonical_job_url(source_url or payload.get("job_url") or payload.get("url"))

        posted_date = str(payload.get("posted_date") or "").strip() or None
        parsed_posted_at = parse_job_posted_at(posted_date, reference_time=now) if posted_date else None
        # A missing/unparseable date on a later crawl must not erase a date that
        # was successfully captured in an earlier crawl of the same job.
        effective_posted_date = posted_date or (existing or {}).get("posted_date")
        effective_posted_at = parsed_posted_at or (existing or {}).get("posted_at")

        doc = JobCurrentDocument(
            _id=job_id,
            job_id=job_id,
            target_id=target_id,
            source_url=source_url,
            job_url=payload.get("job_url") or payload.get("url"),
            apply_url=payload.get("apply_url"),
            title=payload.get("title"),
            company=payload.get("company"),
            location_text=payload.get("location_text") or payload.get("location"),
            employment_type=payload.get("employment_type"),
            duration=payload.get("duration"),
            compensation_text=payload.get("compensation_text")
            or payload.get("salary_text"),
            posted_date=effective_posted_date,
            posted_at=effective_posted_at,
            summary=payload.get("summary") or payload.get("description"),
            responsibilities=as_str_list(payload.get("responsibilities")),
            required_skills=as_str_list(payload.get("required_skills")),
            preferred_skills=as_str_list(payload.get("preferred_skills")),
            job_reference=payload.get("job_reference") or payload.get("reference"),
            content_hash=content_hash,
            first_seen_at=(existing or {}).get("first_seen_at") or now,
            last_seen_at=now,
            last_run_session_id=run_session_id,
            missing_count=0,
            missing_complete_run_count=0,
            missing_since=None,
            deactivation_run_id=None,
            reactivated_at=(
                now
                if existing is not None and existing.get("is_active") is False
                else (existing or {}).get("reactivated_at")
            ),
            is_active=True,
            version=version,
            raw_payload=payload,
        )

        data, set_on_insert = _split_mongo_update_payload(doc)
        data.update({
            "canonical_job_url": canonical_source_url,
            "missing_count": 0,
            "missing_complete_run_count": 0,
            "missing_since": None,
            "freshness_status": "active",
            "last_deep_scraped_at": now,
            "inactive_reason": None,
            "deactivated_at": None,
            "deactivation_run_id": None,
        })

        self.db[JobCurrentDocument.collection_name].update_one(
            {"job_id": job_id},
            {
                "$set": data,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

        if changed:
            history_id = f"jobhist_{job_id}_{content_hash[:16]}"

            history_doc = JobHistoryDocument(
                _id=history_id,
                history_id=history_id,
                job_id=job_id,
                target_id=target_id,
                source_url=source_url,
                content_hash=content_hash,
                version=version,
                payload=payload,
                run_session_id=run_session_id,
            )

            self.db[JobHistoryDocument.collection_name].update_one(
                {"history_id": history_id},
                {
                    "$setOnInsert": history_doc.to_mongo(),
                },
                upsert=True,
            )

        raw_id = "jobraw_" + stable_hash(
            {
                "target_id": target_id,
                "run_session_id": run_session_id,
                "content_hash": content_hash,
            }
        )[:32]

        self.upsert_job_raw(
            JobRawExtractionDocument(
                _id=raw_id,
                raw_id=raw_id,
                run_session_id=run_session_id,
                target_id=target_id,
                source_url=source_url,
                payload=payload,
                content_hash=content_hash,
            )
        )

        return job_id, existing is None, changed

    def upsert_jobs(
        self,
        jobs: Iterable[dict[str, Any]],
        *,
        target_id: str,
        run_session_id: str | None = None,
    ) -> dict[str, Any]:
        stats = {
            "input": 0,
            "inserted": 0,
            "changed": 0,
            "unchanged": 0,
            "changed_job_ids": [],
        }

        for job in jobs:
            stats["input"] += 1

            job_id, inserted, changed = self.upsert_job(
                job,
                target_id=target_id,
                run_session_id=run_session_id,
            )

            if inserted:
                stats["inserted"] += 1
                stats["changed_job_ids"].append(job_id)
            elif changed:
                stats["changed"] += 1
                stats["changed_job_ids"].append(job_id)
            else:
                stats["unchanged"] += 1

        return stats

    def upsert_resume_profile(
        self,
        profile_payload: dict[str, Any],
        *,
        run_session_id: str | None = None,
    ) -> tuple[str, bool, bool]:
        payload = to_plain_data(profile_payload)

        sha256 = str(payload.get("sha256") or "")

        if not sha256:
            raise ValueError("Resume profile payload missing sha256")

        resume_id = str(payload.get("resume_id") or make_resume_id_from_sha(sha256))
        content_hash = stable_hash(payload)
        now = utc_now()

        existing = self.db[ResumeProfileCurrentDocument.collection_name].find_one(
            {"resume_id": resume_id}
        )

        changed = existing is None or existing.get("content_hash") != content_hash

        doc = ResumeProfileCurrentDocument(
            _id=resume_id,
            resume_id=resume_id,
            sha256=sha256,
            source_file_name=str(payload.get("source_file_name") or ""),
            contact=payload.get("contact") or {},
            headline=payload.get("headline"),
            summary=payload.get("summary"),
            total_experience_years=payload.get("total_experience_years"),
            current_title=payload.get("current_title"),
            current_company=payload.get("current_company"),
            primary_skills=as_str_list(payload.get("primary_skills")),
            secondary_skills=as_str_list(payload.get("secondary_skills")),
            tools_and_platforms=as_str_list(payload.get("tools_and_platforms")),
            programming_languages=as_str_list(payload.get("programming_languages")),
            domains=as_str_list(payload.get("domains")),
            certifications=as_str_list(payload.get("certifications")),
            experience=payload.get("experience") or [],
            education=payload.get("education") or [],
            projects=payload.get("projects") or [],
            languages=as_str_list(payload.get("languages")),
            raw_ocr_markdown_path=payload.get("raw_ocr_markdown_path"),
            parse_warnings=as_str_list(payload.get("parse_warnings")),
            content_hash=content_hash,
            first_seen_at=(existing or {}).get("first_seen_at") or now,
            last_seen_at=now,
            last_run_session_id=run_session_id,
            is_active=True,
            raw_payload=payload,
        )

        data, set_on_insert = _split_mongo_update_payload(doc)

        self.db[ResumeProfileCurrentDocument.collection_name].update_one(
            {"resume_id": resume_id},
            {
                "$set": data,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

        if changed:
            history_id = f"reshist_{resume_id}_{content_hash[:16]}"

            history_doc = ResumeProfileHistoryDocument(
                _id=history_id,
                history_id=history_id,
                resume_id=resume_id,
                sha256=sha256,
                source_file_name=str(payload.get("source_file_name") or ""),
                content_hash=content_hash,
                payload=payload,
                run_session_id=run_session_id,
            )

            self.db[ResumeProfileHistoryDocument.collection_name].update_one(
                {"history_id": history_id},
                {
                    "$setOnInsert": history_doc.to_mongo(),
                },
                upsert=True,
            )

        return resume_id, existing is None, changed

    def upsert_candidate_tower(
        self,
        record_payload: dict[str, Any],
        *,
        source_content_hash: str | None = None,
    ) -> str:
        payload = to_plain_data(record_payload)

        sha256 = str(payload.get("sha256") or "")
        candidate_id = str(payload.get("candidate_id") or make_candidate_id(sha256))
        resume_id = str(payload.get("resume_id") or make_resume_id_from_sha(sha256))
        source_hash = source_content_hash or stable_hash(payload)

        skills = unique_skills(as_str_list(payload.get("skills")))
        if not skills:
            skills = unique_skills(
                [
                    *as_str_list(payload.get("primary_skills")),
                    *as_str_list(payload.get("secondary_skills")),
                    *as_str_list(payload.get("programming_languages")),
                    *as_str_list(payload.get("tools_and_platforms")),
                    *as_str_list(payload.get("certifications")),
                ]
            )
        if not skills and payload.get("raw_payload"):
            raw_payload = payload.get("raw_payload")
            if isinstance(raw_payload, dict):
                skills = build_canonical_candidate_skills(raw_payload)

        skills_text = str(payload.get("skills_text") or "")
        if skills and not skills_text.strip():
            skills_text = "Skills: " + ", ".join(skills)

        doc = CandidateTowerDocument(
            _id=candidate_id,
            candidate_id=candidate_id,
            resume_id=resume_id,
            source_file_name=str(payload.get("source_file_name") or ""),
            sha256=sha256,
            full_name=payload.get("full_name"),
            email=payload.get("email"),
            phone=payload.get("phone"),
            location=payload.get("location"),
            current_title=payload.get("current_title"),
            current_company=payload.get("current_company"),
            total_experience_years=payload.get("total_experience_years"),
            skills=skills,
            primary_skills=[],
            secondary_skills=[],
            domains=as_str_list(payload.get("domains")),
            identity_text=str(payload.get("identity_text") or ""),
            skills_text=skills_text,
            experience_text=str(payload.get("experience_text") or ""),
            education_text=str(payload.get("education_text") or ""),
            candidate_embedding_text=str(
                payload.get("candidate_embedding_text") or ""
            ),
            source_content_hash=source_hash,
        )

        data, set_on_insert = _split_mongo_update_payload(doc)

        self.db[CandidateTowerDocument.collection_name].update_one(
            {"candidate_id": candidate_id},
            {
                "$set": data,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

        return candidate_id

    def upsert_job_tower(self, doc: JobTowerDocument) -> str:
        data, set_on_insert = _split_mongo_update_payload(doc)

        existing = self.db[JobTowerDocument.collection_name].find_one(
            {"job_id": doc.job_id}
        )

        if existing:
            # A changed job-tower text must be indexed again. Preserving an
            # already-indexed status would leave Qdrant stale after a portal
            # refresh changes title, skills, summary, company, or location.
            source_changed = str(existing.get("source_content_hash") or "") != str(doc.source_content_hash or "")
            if source_changed:
                data["embedding_status"] = "pending"
                data["embedding_model"] = None
                data["last_indexed_at"] = None
            else:
                data["embedding_status"] = existing.get("embedding_status", "pending")
                data["embedding_model"] = existing.get("embedding_model")
                data["last_indexed_at"] = existing.get("last_indexed_at")

        self.db[JobTowerDocument.collection_name].update_one(
            {"job_id": doc.job_id},
            {
                "$set": data,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

        return doc.job_tower_id

    def mark_tower_indexed(
        self,
        *,
        record_type: str,
        record_id: str,
        collection_name: str,
        embedding_model: str,
        source_content_hash: str,
        vector_size: int,
        qdrant_point_id: str,
    ) -> None:
        now = utc_now()
        index_id = f"{record_type}:{record_id}:{embedding_model}"

        doc = QdrantIndexStateDocument(
            _id=index_id,
            index_id=index_id,
            record_type=record_type,  # type: ignore[arg-type]
            record_id=record_id,
            collection_name_value=collection_name,
            embedding_model=embedding_model,
            source_content_hash=source_content_hash,
            vector_size=vector_size,
            qdrant_point_id=qdrant_point_id,
            indexed_at=now,
        )

        data, set_on_insert = _split_mongo_update_payload(doc)

        self.db[QdrantIndexStateDocument.collection_name].update_one(
            {"index_id": index_id},
            {
                "$set": data,
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )

        if record_type == "job":
            tower_collection = JobTowerDocument.collection_name
            key = "job_id"
        else:
            tower_collection = CandidateTowerDocument.collection_name
            key = "candidate_id"

        self.db[tower_collection].update_one(
            {key: record_id},
            {
                "$set": {
                    "embedding_status": "indexed",
                    "embedding_model": embedding_model,
                    "last_indexed_at": now,
                    "updated_at": now,
                }
            },
        )

    def upsert_matches(self, matches: list[CandidateJobMatchDocument]) -> None:
        if not matches:
            return

        operations: list[UpdateOne] = []

        for match in matches:
            data, set_on_insert = _split_mongo_update_payload(match)

            operations.append(
                UpdateOne(
                    {
                        "match_run_id": match.match_run_id,
                        "candidate_id": match.candidate_id,
                        "job_id": match.job_id,
                    },
                    {
                        "$set": data,
                        "$setOnInsert": set_on_insert,
                    },
                    upsert=True,
                )
            )

        self.db[CandidateJobMatchDocument.collection_name].bulk_write(
            operations,
            ordered=False,
        )

    def counts(self) -> dict[str, int]:
        names = [
            "warehouse_run_sessions",
            "job_raw_extractions",
            "jobs_current",
            "jobs_history",
            "resume_profiles_current",
            "resume_profiles_history",
            "job_tower_records",
            "candidate_tower_records",
            "qdrant_index_state",
            "candidate_job_matches",
        ]

        return {
            name: self.db[name].count_documents({})
            for name in names
        }


    def reset_missing_state_for_discovered_urls(
        self,
        *,
        target_id: str,
        run_session_id: str | None,
        discovered_urls: list[str],
    ) -> dict[str, int]:
        normalized_urls = canonical_job_urls(discovered_urls)
        if not normalized_urls:
            return {"matched_existing": 0, "modified_existing": 0}
        matched_ids: set[str] = set()
        for doc in self._jobs_current().find({"target_id": target_id}, {"job_id": 1, "job_url": 1, "source_url": 1, "apply_url": 1, "canonical_job_url": 1}):
            doc_keys = {
                canonical_job_url(doc.get("canonical_job_url")),
                canonical_job_url(doc.get("job_url")),
                canonical_job_url(doc.get("source_url")),
                canonical_job_url(doc.get("apply_url")),
            }
            if set(normalized_urls).intersection({key for key in doc_keys if key}):
                matched_ids.add(str(doc.get("job_id") or doc.get("_id")))

        if not matched_ids:
            return {"matched_existing": 0, "modified_existing": 0}
        result = self._jobs_current().update_many(
            {"job_id": {"$in": sorted(matched_ids)}},
            {
                "$set": {
                    "last_seen_at": utc_now(),
                    "last_run_session_id": run_session_id,
                    "freshness_status": "active",
                    "missing_count": 0,
                    "missing_complete_run_count": 0,
                    "is_active": True,
                    "updated_at": utc_now(),
                },
                "$unset": {
                    "inactive_reason": "",
                    "deactivated_at": "",
                    "deactivation_run_id": "",
                    "missing_since": "",
                },
            },
        )
        return {"matched_existing": len(matched_ids), "modified_existing": int(result.modified_count)}

    def active_jobs(
        self,
        *,
        limit: int | None = None,
        only_pending_tower: bool = False,
        job_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"is_active": True}
        normalized_job_ids = [str(job_id) for job_id in (job_ids or []) if str(job_id).strip()]
        if normalized_job_ids:
            query["job_id"] = {"$in": normalized_job_ids}

        cursor = self.db[JobCurrentDocument.collection_name].find(query).sort("last_seen_at", -1)

        if not only_pending_tower:
            if limit:
                cursor = cursor.limit(limit)
            return list(cursor)

        out: list[dict[str, Any]] = []
        towers = self.db[JobTowerDocument.collection_name]
        for job in cursor:
            tower = towers.find_one({"job_id": job.get("job_id")}, {"source_content_hash": 1})
            if not tower or str(tower.get("source_content_hash") or "") != str(job.get("content_hash") or ""):
                out.append(job)
                if limit and len(out) >= int(limit):
                    break
        return out

    def active_resume_profiles(
        self,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        cursor = self.db[ResumeProfileCurrentDocument.collection_name].find(
            {"is_active": True}
        ).sort("last_seen_at", -1)

        if limit:
            cursor = cursor.limit(limit)

        return list(cursor)

    def job_towers(
        self,
        *,
        only_pending: bool = False,
        limit: int | None = None,
        job_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if only_pending:
            query["embedding_status"] = {"$ne": "indexed"}
        if job_ids:
            query["job_id"] = {"$in": [str(job_id) for job_id in job_ids if str(job_id).strip()]}

        cursor = self.db[JobTowerDocument.collection_name].find(query).sort(
            "updated_at",
            -1,
        )

        if limit:
            cursor = cursor.limit(limit)

        return list(cursor)

    def candidate_towers(
        self,
        *,
        only_pending: bool = False,
        limit: int | None = None,
        candidate_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}

        if only_pending:
            query["embedding_status"] = {"$ne": "indexed"}

        if candidate_id:
            query["candidate_id"] = candidate_id

        cursor = self.db[CandidateTowerDocument.collection_name].find(query).sort(
            "updated_at",
            -1,
        )

        if limit:
            cursor = cursor.limit(limit)

        return list(cursor)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.db[JobCurrentDocument.collection_name].find_one(
            {"job_id": job_id}
        )
