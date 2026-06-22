from __future__ import annotations

from typing import Any, Iterable

from pymongo import UpdateOne
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

        job_id = make_job_id(payload, target_id=target_id)
        # Portal freshness strings (for example, "3 days ago") change on every
        # crawl even when the job itself has not changed. Keep them out of the
        # version/history hash while still persisting them on the current record.
        content_hash_payload = dict(payload)
        content_hash_payload.pop("posted_date", None)
        content_hash = stable_hash(content_hash_payload)
        now = utc_now()

        existing = self.db[JobCurrentDocument.collection_name].find_one(
            {"job_id": job_id}
        )

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
            is_active=True,
            version=version,
            raw_payload=payload,
        )

        data, set_on_insert = _split_mongo_update_payload(doc)

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
    ) -> dict[str, int]:
        stats = {
            "input": 0,
            "inserted": 0,
            "changed": 0,
            "unchanged": 0,
        }

        for job in jobs:
            stats["input"] += 1

            _, inserted, changed = self.upsert_job(
                job,
                target_id=target_id,
                run_session_id=run_session_id,
            )

            if inserted:
                stats["inserted"] += 1
            elif changed:
                stats["changed"] += 1
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

    def active_jobs(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        cursor = self.db[JobCurrentDocument.collection_name].find(
            {"is_active": True}
        ).sort("last_seen_at", -1)

        if limit:
            cursor = cursor.limit(limit)

        return list(cursor)

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