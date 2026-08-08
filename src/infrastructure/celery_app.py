from __future__ import annotations

import os

from celery import Celery
from kombu import Exchange, Queue

from ..common.constants import EnvironmentVariables
from ..common.env import load_runtime_env
from .scrape_queue_topology import (
    DEAD_LETTER,
    FLEET_CONTROL,
    JOB_DETAIL,
    JOB_PERSISTENCE,
    RECONCILIATION,
    RETRY,
    SOURCE_DISCOVERY,
    ScrapeQueueTopology,
)

load_runtime_env()

REDIS_URL = os.getenv(EnvironmentVariables.REDIS_URL, "redis://localhost:6379/0")
BROKER_URL = os.getenv("JOB_MINER_CELERY_BROKER_URL", REDIS_URL)
RESULT_BACKEND = os.getenv("JOB_MINER_CELERY_RESULT_BACKEND", REDIS_URL)

RECOMMENDATION_QUEUE = os.getenv("JOB_MINER_RECOMMENDATION_QUEUE", "recommendation_queue")
RESUME_PROCESSING_QUEUE = os.getenv("JOB_MINER_RESUME_PROCESSING_QUEUE", "resume_processing_queue")
RECOMMENDATION_REFRESH_QUEUE = os.getenv("JOB_MINER_RECOMMENDATION_REFRESH_QUEUE", "recommendation_refresh_queue")
APPLICATION_ORCHESTRATION_QUEUE = os.getenv("JOB_MINER_APPLICATION_ORCHESTRATION_QUEUE", "application_orchestration_queue")
APPLICATION_JOB_QUEUE = os.getenv("JOB_MINER_APPLICATION_JOB_QUEUE", "application_job_queue")

SCRAPE_QUEUE_TOPOLOGY = ScrapeQueueTopology.from_environment()
FLEET_CONTROL_QUEUE = SCRAPE_QUEUE_TOPOLOGY.queue_name(FLEET_CONTROL)
SOURCE_DISCOVERY_QUEUE = SCRAPE_QUEUE_TOPOLOGY.queue_name(SOURCE_DISCOVERY)
JOB_DETAIL_QUEUE = SCRAPE_QUEUE_TOPOLOGY.queue_name(JOB_DETAIL)
JOB_PERSISTENCE_QUEUE = SCRAPE_QUEUE_TOPOLOGY.queue_name(JOB_PERSISTENCE)
RECONCILIATION_QUEUE = SCRAPE_QUEUE_TOPOLOGY.queue_name(RECONCILIATION)
SCRAPE_RETRY_QUEUE = SCRAPE_QUEUE_TOPOLOGY.queue_name(RETRY)
SCRAPE_DEAD_LETTER_QUEUE = SCRAPE_QUEUE_TOPOLOGY.queue_name(DEAD_LETTER)

# Compatibility aliases keep the admin portal API and existing task imports
# stable while moving their physical workloads to the Phase 8.2 queues.
PORTAL_PROBE_QUEUE = SOURCE_DISCOVERY_QUEUE
PORTAL_SCRAPE_QUEUE = JOB_DETAIL_QUEUE
PORTAL_SCHEDULER_QUEUE = FLEET_CONTROL_QUEUE
PORTAL_ARTIFACT_QUEUE = JOB_PERSISTENCE_QUEUE


def _declared_queue_names() -> tuple[str, ...]:
    names = (
        "default",
        "maintenance_queue",
        RECOMMENDATION_QUEUE,
        RESUME_PROCESSING_QUEUE,
        RECOMMENDATION_REFRESH_QUEUE,
        APPLICATION_ORCHESTRATION_QUEUE,
        APPLICATION_JOB_QUEUE,
        *SCRAPE_QUEUE_TOPOLOGY.queue_names(),
    )
    if len(set(names)) != len(names):
        raise ValueError(
            "Celery queue names must be unique across scraping and application workloads"
        )
    return names


def _durable_queue(name: str) -> Queue:
    exchange = Exchange(name, type="direct", durable=True)
    return Queue(name, exchange=exchange, routing_key=name, durable=True)

celery_app = Celery(
    "job_miner",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=[
        "src.tasks.mvp_tasks",
        "src.tasks.recommendation_tasks",
        "src.tasks.resume_tasks",
        "src.tasks.portal_tasks",
        "src.tasks.application_tasks",
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
    task_default_exchange="default",
    task_default_exchange_type="direct",
    task_default_routing_key="default",
    task_create_missing_queues=False,
    task_queues=tuple(_durable_queue(name) for name in _declared_queue_names()),
    task_routes={
        "src.tasks.mvp_tasks.full_demo_pipeline_task": {"queue": "maintenance_queue"},
        "src.tasks.mvp_tasks.run_cli_task": {"queue": "maintenance_queue"},
        "src.tasks.recommendation_tasks.generate_recommendations_after_resume_upload_task": {
            "queue": RECOMMENDATION_QUEUE
        },
        "src.tasks.resume_tasks.process_candidate_resume_upload_task": {
            "queue": RESUME_PROCESSING_QUEUE
        },
        **SCRAPE_QUEUE_TOPOLOGY.task_routes(),
        "src.tasks.recommendation_tasks.process_recommendation_refresh_batch_task": {"queue": RECOMMENDATION_REFRESH_QUEUE},
        "src.tasks.application_tasks.run_application_batch_task": {"queue": APPLICATION_ORCHESTRATION_QUEUE},
        "src.tasks.application_tasks.run_single_job_application_task": {"queue": APPLICATION_JOB_QUEUE},
    },
)


if os.getenv("JOB_MINER_ENABLE_PORTAL_BEAT", "0").lower() in {"1", "true", "yes"}:
    celery_app.conf.beat_schedule = {
        **getattr(celery_app.conf, "beat_schedule", {}),
        "schedule-due-job-portals": {
            "task": "src.tasks.portal_tasks.schedule_due_job_portals_task",
            "schedule": int(os.getenv("JOB_MINER_PORTAL_SCHEDULER_INTERVAL_SECONDS", "300")),
            "options": {"queue": PORTAL_SCHEDULER_QUEUE},
        },
        "process-recommendation-refresh-batch": {
            "task": "src.tasks.recommendation_tasks.process_recommendation_refresh_batch_task",
            "schedule": int(os.getenv("JOB_MINER_RECOMMENDATION_REFRESH_INTERVAL_SECONDS", "900")),
            "options": {"queue": RECOMMENDATION_REFRESH_QUEUE},
        },
    }
