from __future__ import annotations

import os

from celery import Celery

from ..common.env import load_runtime_env
from ..common.constants import EnvironmentVariables

load_runtime_env()

REDIS_URL = os.getenv(EnvironmentVariables.REDIS_URL, "redis://localhost:6379/0")

celery_app = Celery(
    "job_miner",
    broker=REDIS_URL,
    backend=os.getenv("JOB_MINER_CELERY_RESULT_BACKEND", REDIS_URL),
    include=["src.tasks.mvp_tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "src.tasks.mvp_tasks.full_demo_pipeline_task": {"queue": "maintenance_queue"},
        "src.tasks.mvp_tasks.run_cli_task": {"queue": "maintenance_queue"},
    },
)
