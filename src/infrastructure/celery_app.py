from __future__ import annotations

import os

from celery import Celery

from ..common.constants import EnvironmentVariables
from ..common.env import load_runtime_env

load_runtime_env()

REDIS_URL = os.getenv(EnvironmentVariables.REDIS_URL, "redis://localhost:6379/0")
BROKER_URL = os.getenv("JOB_MINER_CELERY_BROKER_URL", REDIS_URL)
RESULT_BACKEND = os.getenv("JOB_MINER_CELERY_RESULT_BACKEND", REDIS_URL)

RECOMMENDATION_QUEUE = os.getenv("JOB_MINER_RECOMMENDATION_QUEUE", "recommendation_queue")

celery_app = Celery(
    "job_miner",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=[
        "src.tasks.mvp_tasks",
        "src.tasks.recommendation_tasks",
    ],
)

celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    result_expires=60 * 60 * 24,
    task_default_queue="default",
    task_routes={
        "src.tasks.mvp_tasks.full_demo_pipeline_task": {"queue": "maintenance_queue"},
        "src.tasks.mvp_tasks.run_cli_task": {"queue": "maintenance_queue"},
        "src.tasks.recommendation_tasks.generate_recommendations_after_resume_upload_task": {
            "queue": RECOMMENDATION_QUEUE
        },
    },
)
