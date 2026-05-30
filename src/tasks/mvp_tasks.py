from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from celery import current_task

from ..control.postgres import postgres_session
from ..control.repository import ControlRepository
from ..infrastructure.celery_app import celery_app

REPO_ROOT = Path(os.getenv("JOB_MINER_REPO_ROOT", ".")).resolve()


def _run_command(args: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    proc = subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result = {
        "command": " ".join(args),
        "returncode": proc.returncode,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-8000:],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    if proc.returncode != 0:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def _update(status: str, *, result: dict[str, Any] | None = None, error: BaseException | None = None, event: str | None = None) -> None:
    task_id = current_task.request.id if current_task else None
    if not task_id:
        return
    with postgres_session() as session:
        repo = ControlRepository(session)
        repo.update_task_status(task_id, status, result=result, error=error)
        if event:
            repo.add_task_event(task_id, event, payload=result or {})


@celery_app.task(bind=True, name="src.tasks.mvp_tasks.run_cli_task")
def run_cli_task(self, command: list[str]) -> dict[str, Any]:
    _update("running", event="task_started")
    try:
        result = _run_command(command)
        _update("completed", result=result, event="task_completed")
        return result
    except Exception as exc:
        _update("failed", error=exc, event="task_failed")
        raise


@celery_app.task(bind=True, name="src.tasks.mvp_tasks.full_demo_pipeline_task")
def full_demo_pipeline_task(self, pipeline_run_id: str | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    task_uuid = self.request.id
    with postgres_session() as session:
        repo = ControlRepository(session)
        if pipeline_run_id:
            repo.start_pipeline_run(pipeline_run_id)
        repo.update_task_status(task_uuid, "running")
        repo.add_task_event(task_uuid, "pipeline_started")

    recreate = "--recreate" if options.get("recreate_qdrant", True) else ""
    candidates_limit = options.get("candidates_limit")
    candidates_limit_args = ["--candidates-limit", str(candidates_limit)] if candidates_limit else []
    llm_top_k = str(options.get("llm_top_k", 100))
    final_top_n = str(options.get("final_top_n", 10))
    run_llm = bool(options.get("run_llm", True))

    steps: list[tuple[str, list[str]]] = [
        ("warehouse_health", [sys.executable, "-m", "src.warehouse.cli", "--root", ".", "health"]),
        ("init_indexes", [sys.executable, "-m", "src.warehouse.cli", "--root", ".", "init-indexes"]),
        ("backfill_jobs", [sys.executable, "-m", "src.warehouse.cli", "--root", ".", "backfill-jobs", "--jobs-dir", "data/processed"]),
        ("backfill_resumes", [sys.executable, "-m", "src.warehouse.cli", "--root", ".", "backfill-resumes", "--resumes-dir", "data/resumes/processed"]),
        ("build_job_tower", [sys.executable, "-m", "src.warehouse.cli", "--root", ".", "build-job-tower"]),
        ("build_candidate_tower", [sys.executable, "-m", "src.warehouse.cli", "--root", ".", "build-candidate-tower"]),
        ("index_jobs", [sys.executable, "-u", "-m", "src.matching.cli", "--root", ".", "index-jobs-from-mongo"] + ([recreate] if recreate else [])),
        ("index_candidates", [sys.executable, "-u", "-m", "src.matching.cli", "--root", ".", "index-candidates-from-mongo"] + ([recreate] if recreate else [])),
        ("baseline_match", [sys.executable, "-u", "-m", "src.matching.cli", "--root", ".", "match-candidates-from-mongo", "--top-n", "10"]),
    ]
    if run_llm:
        steps.extend([
            ("init_llm_indexes", [sys.executable, "-m", "src.matching.llm_rerank_cli", "--root", ".", "init-optimization-indexes"]),
            (
                "llm_rerank",
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "src.matching.llm_rerank_cli",
                    "--root",
                    ".",
                    "match",
                    "--top-k",
                    llm_top_k,
                    "--final-top-n",
                    final_top_n,
                    "--llm-model",
                    os.getenv("JOB_MINER_LLM_RERANKER_MODEL", "qwen2.5:3b"),
                    "--llm-chunk-size",
                    os.getenv("JOB_MINER_LLM_RERANKER_CHUNK_SIZE", "3"),
                    "--llm-num-ctx",
                    os.getenv("JOB_MINER_LLM_RERANKER_NUM_CTX", "8192"),
                    "--llm-num-predict",
                    os.getenv("JOB_MINER_LLM_RERANKER_NUM_PREDICT", "4096"),
                ]
                + candidates_limit_args,
            ),
        ])
    steps.append(("compare", [sys.executable, "scripts/compare_baseline_vs_llm.py"]))

    outputs: list[dict[str, Any]] = []
    try:
        for idx, (name, command) in enumerate(steps, start=1):
            with postgres_session() as session:
                repo = ControlRepository(session)
                repo.add_task_event(task_uuid, f"step_started:{name}", progress_percent=round((idx - 1) / len(steps) * 100, 2), payload={"command": command})
            result = _run_command(command)
            outputs.append({"step": name, **result})
            with postgres_session() as session:
                repo = ControlRepository(session)
                repo.add_task_event(task_uuid, f"step_completed:{name}", progress_percent=round(idx / len(steps) * 100, 2), payload=result)
        summary = {"steps": len(steps), "outputs": outputs[-3:]}
        with postgres_session() as session:
            repo = ControlRepository(session)
            repo.update_task_status(task_uuid, "completed", result=summary)
            repo.add_task_event(task_uuid, "pipeline_completed", progress_percent=100, payload=summary)
            if pipeline_run_id:
                repo.complete_pipeline_run(pipeline_run_id, "completed", metrics=summary)
        return summary
    except Exception as exc:
        with postgres_session() as session:
            repo = ControlRepository(session)
            repo.update_task_status(task_uuid, "failed", error=exc)
            repo.add_task_event(task_uuid, "pipeline_failed", payload={"error": str(exc)})
            if pipeline_run_id:
                repo.complete_pipeline_run(pipeline_run_id, "failed", error_message=str(exc))
        raise
