from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from .gpu_monitor import query_gpu_snapshot
from .logger import SessionLogger
from .ocr.backends.factory import build_ocr_backend
from .ocr.preprocess import extract_native_docx_text, extract_native_pdf_text
from .parse.quality import estimate_ocr_quality
from .parse.resume_extractor import ResumeExtractor
from .prepare.candidate_tower import build_candidate_tower_record
from .schemas import CandidateTowerRecord, OcrDocument, ResumeProfile, RunResult, SystemConfig
from .store.storefront import (
    load_processed_hashes,
    save_candidate_tower_records,
    save_failed_payload,
    save_ocr_document,
    save_resume_profile,
    save_run_summary,
)
from .utils import SUPPORTED_EXTENSIONS, compact_text, ensure_dir, list_resume_files, sha256_file


def _new_run_session_id() -> str:
    return "resume_ocr_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resume_id_from_hash(sha256: str) -> str:
    return "res_" + sha256[:24]


def _native_text_doc(
    source_path: Path,
    *,
    resume_id: str,
    sha256: str,
    markdown: str,
    backend: str,
    raw_source: str,
) -> OcrDocument:
    now = datetime.now(timezone.utc)
    return OcrDocument(
        source_path=str(source_path),
        resume_id=resume_id,
        sha256=sha256,
        file_name=source_path.name,
        file_ext=source_path.suffix.lower(),
        backend=backend,
        used_native_text=True,
        markdown=markdown,
        raw_result={"source": raw_source},
        started_at=now,
        completed_at=now,
        elapsed_seconds=0.0,
    )


class ResumeOcrPipeline:
    def __init__(self, root: Path, config: SystemConfig, logger: SessionLogger):
        self.root = root
        self.config = config
        self.logger = logger
        self.output_dir = root / config.output.dir
        self.failed_dir = root / config.output.failed_dir
        self.ocr_backend = build_ocr_backend(config.ocr, root)
        self.extractor = ResumeExtractor(
            config.llm,
            max_markdown_chars=config.parser.max_markdown_chars_for_extraction,
        )

    def _ocr_or_native(self, source_path: Path, *, resume_id: str, sha256: str) -> OcrDocument:
        suffix = source_path.suffix.lower()

        if suffix == ".docx":
            native_text = extract_native_docx_text(source_path)
            if len(native_text) < self.config.parser.min_markdown_chars:
                raise ValueError(f"DOCX native text too short: {len(native_text)} chars")
            self.logger.log("native_docx_text_used", source_path=str(source_path), chars=len(native_text))
            return _native_text_doc(
                source_path,
                resume_id=resume_id,
                sha256=sha256,
                markdown=native_text,
                backend="native_docx_text",
                raw_source="embedded_docx_text",
            )

        if self.config.ocr.prefer_native_pdf_text and suffix == ".pdf":
            native_text = extract_native_pdf_text(source_path, self.config.ocr.max_pages)
            if len(native_text) >= self.config.ocr.min_native_text_chars:
                self.logger.log("native_pdf_text_used", source_path=str(source_path), chars=len(native_text))
                return _native_text_doc(
                    source_path,
                    resume_id=resume_id,
                    sha256=sha256,
                    markdown=native_text,
                    backend="native_pdf_text",
                    raw_source="embedded_pdf_text",
                )

        self.logger.log("ocr_start", source_path=str(source_path), backend=self.config.ocr.backend)
        self.logger.log("gpu_before_ocr", source_path=str(source_path), gpu=query_gpu_snapshot())
        doc = self.ocr_backend.parse(source_path, resume_id=resume_id, sha256=sha256)
        self.logger.log("gpu_after_ocr", source_path=str(source_path), gpu=query_gpu_snapshot(), elapsed_seconds=doc.elapsed_seconds)
        return doc

    def process_file(self, source_path: Path) -> tuple[ResumeProfile | None, CandidateTowerRecord | None, str]:
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        if source_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            self.logger.log("unsupported_file_skipped", source_path=str(source_path), suffix=source_path.suffix)
            return None, None, "skipped"

        sha256 = sha256_file(source_path)
        resume_id = _resume_id_from_hash(sha256)
        self.logger.log("file_start", source_path=str(source_path), resume_id=resume_id, sha256=sha256)

        try:
            ocr_doc = self._ocr_or_native(source_path, resume_id=resume_id, sha256=sha256)
            ocr_paths = save_ocr_document(self.output_dir, ocr_doc)
            quality = estimate_ocr_quality(ocr_doc.markdown)
            self.logger.log("ocr_quality", source_path=str(source_path), resume_id=resume_id, **quality)

            if len(compact_text(ocr_doc.markdown)) < self.config.parser.min_markdown_chars:
                raise ValueError(f"OCR markdown too short: {len(ocr_doc.markdown)} chars")

            if not self.config.parser.enabled:
                return None, None, "processed"

            self.logger.log("resume_parse_start", source_path=str(source_path), resume_id=resume_id)
            profile = self.extractor.extract(
                ocr_doc.markdown,
                resume_id=resume_id,
                file_name=source_path.name,
                sha256=sha256,
                ocr_markdown_path=str(ocr_paths["markdown_path"]),
            )
            profile_path = save_resume_profile(self.output_dir, profile)
            record = build_candidate_tower_record(profile)
            self.logger.log(
                "resume_parse_complete",
                source_path=str(source_path),
                resume_id=resume_id,
                profile_path=str(profile_path),
                candidate_id=record.candidate_id,
            )
            return profile, record, "processed"
        except Exception as exc:
            failed_path = save_failed_payload(self.failed_dir, resume_id, source_path.name, "error", repr(exc))
            self.logger.log("file_failed", source_path=str(source_path), resume_id=resume_id, error_message=str(exc), failed_path=str(failed_path))
            return None, None, "failed"

    async def process_files(self, files: list[Path], *, skip_existing: bool = True) -> RunResult:
        run_t0 = time.perf_counter()
        run_session_id = self.logger.session_id
        ensure_dir(self.output_dir)

        processed_hashes = load_processed_hashes(self.output_dir) if skip_existing else set()
        attempted = 0
        processed = 0
        skipped = 0
        failed = 0
        candidate_records: list[CandidateTowerRecord] = []

        semaphore = asyncio.Semaphore(self.config.ocr.ocr_concurrency)

        async def run_one(path: Path):
            nonlocal attempted, processed, skipped, failed
            sha = sha256_file(path)
            if skip_existing and sha in processed_hashes:
                skipped += 1
                self.logger.log("existing_file_skipped", source_path=str(path), sha256=sha)
                return None
            attempted += 1
            async with semaphore:
                profile, record, status = await asyncio.to_thread(self.process_file, path)
                if status == "processed":
                    processed += 1
                elif status == "skipped":
                    skipped += 1
                elif status == "failed":
                    failed += 1
                return record

        results = await asyncio.gather(*(run_one(path) for path in files), return_exceptions=True)
        for item in results:
            if isinstance(item, Exception):
                failed += 1
                self.logger.log("task_exception", error_message=str(item))
            elif item is not None:
                candidate_records.append(item)

        candidate_tower_path = None
        if candidate_records:
            _, latest_path = save_candidate_tower_records(self.output_dir, candidate_records, run_session_id)
            candidate_tower_path = str(latest_path)

        elapsed = round(time.perf_counter() - run_t0, 3)
        payload = {
            "run_session_id": run_session_id,
            "attempted_files": attempted,
            "processed_files": processed,
            "skipped_files": skipped,
            "failed_files": failed,
            "candidate_records": len(candidate_records),
            "candidate_tower_path": candidate_tower_path,
            "elapsed_seconds": elapsed,
        }
        summary_path = save_run_summary(self.output_dir, payload, run_session_id)

        return RunResult(
            run_session_id=run_session_id,
            input_path="",
            attempted_files=attempted,
            processed_files=processed,
            skipped_files=skipped,
            failed_files=failed,
            output_dir=str(self.output_dir),
            candidate_tower_path=candidate_tower_path,
            summary_path=str(summary_path),
            elapsed_seconds=elapsed,
        )


async def run_file(root: Path, config: SystemConfig, logger: SessionLogger, input_path: Path, *, skip_existing: bool = False) -> RunResult:
    pipeline = ResumeOcrPipeline(root, config, logger)
    result = await pipeline.process_files([input_path], skip_existing=skip_existing)
    result.input_path = str(input_path)
    return result


async def run_dir(root: Path, config: SystemConfig, logger: SessionLogger, input_dir: Path, *, skip_existing: bool = True) -> RunResult:
    files = list_resume_files(input_dir)
    logger.log("directory_discovered", input_path=str(input_dir), files=len(files))
    pipeline = ResumeOcrPipeline(root, config, logger)
    result = await pipeline.process_files(files, skip_existing=skip_existing)
    result.input_path = str(input_dir)
    return result
