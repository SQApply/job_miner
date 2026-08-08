from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Mapping


PHASE8_2_QUEUE_CONTRACT_VERSION = "1.0"

FLEET_CONTROL = "fleet_control"
SOURCE_DISCOVERY = "source_discovery"
JOB_DETAIL = "job_detail"
JOB_PERSISTENCE = "job_persistence"
RECONCILIATION = "reconciliation"
RETRY = "retry"
DEAD_LETTER = "dead_letter"

EXPECTED_LOGICAL_QUEUES = (
    FLEET_CONTROL,
    SOURCE_DISCOVERY,
    JOB_DETAIL,
    JOB_PERSISTENCE,
    RECONCILIATION,
    RETRY,
    DEAD_LETTER,
)


@dataclass(frozen=True)
class ScrapeQueueLane:
    logical_name: str
    queue_name: str
    worker_group: str
    workload: str
    browser_bound: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ScrapeQueueTopology:
    """Physical Celery queue names for the Phase 8.2 isolation contract.

    The logical names are fixed by the production plan. Physical queue names
    may be overridden with environment variables, but must remain unique so a
    slow browser workload cannot share a worker lane with control or state
    transitions by accident.
    """

    lanes: tuple[ScrapeQueueLane, ...]
    contract_version: str = PHASE8_2_QUEUE_CONTRACT_VERSION

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ScrapeQueueTopology":
        env = os.environ if environment is None else environment

        def queue_name(new_key: str, default: str, legacy_key: str | None = None) -> str:
            value = str(env.get(new_key) or "").strip()
            if not value and legacy_key:
                value = str(env.get(legacy_key) or "").strip()
            return value or default

        topology = cls(
            lanes=(
                ScrapeQueueLane(
                    logical_name=FLEET_CONTROL,
                    queue_name=queue_name(
                        "JOB_MINER_FLEET_CONTROL_QUEUE",
                        FLEET_CONTROL,
                        "JOB_MINER_PORTAL_SCHEDULER_QUEUE",
                    ),
                    worker_group="control",
                    workload="fleet admission, scheduling, and operational control",
                ),
                ScrapeQueueLane(
                    logical_name=SOURCE_DISCOVERY,
                    queue_name=queue_name(
                        "JOB_MINER_SOURCE_DISCOVERY_QUEUE",
                        SOURCE_DISCOVERY,
                        "JOB_MINER_PORTAL_PROBE_QUEUE",
                    ),
                    worker_group="source_discovery",
                    workload="listing acquisition, route resolution, and candidate discovery",
                    browser_bound=True,
                ),
                ScrapeQueueLane(
                    logical_name=JOB_DETAIL,
                    queue_name=queue_name(
                        "JOB_MINER_JOB_DETAIL_QUEUE",
                        JOB_DETAIL,
                        "JOB_MINER_PORTAL_SCRAPE_QUEUE",
                    ),
                    worker_group="job_detail",
                    workload="bounded browser detail extraction and model fallback",
                    browser_bound=True,
                ),
                ScrapeQueueLane(
                    logical_name=JOB_PERSISTENCE,
                    queue_name=queue_name(
                        "JOB_MINER_JOB_PERSISTENCE_QUEUE",
                        JOB_PERSISTENCE,
                        "JOB_MINER_PORTAL_ARTIFACT_QUEUE",
                    ),
                    worker_group="state",
                    workload="MongoDB persistence, tower updates, and changed-only indexing",
                ),
                ScrapeQueueLane(
                    logical_name=RECONCILIATION,
                    queue_name=queue_name(
                        "JOB_MINER_RECONCILIATION_QUEUE",
                        RECONCILIATION,
                    ),
                    worker_group="state",
                    workload="complete-snapshot lifecycle reconciliation",
                ),
                ScrapeQueueLane(
                    logical_name=RETRY,
                    queue_name=queue_name("JOB_MINER_SCRAPE_RETRY_QUEUE", RETRY),
                    worker_group="recovery",
                    workload="bounded retry dispatch",
                ),
                ScrapeQueueLane(
                    logical_name=DEAD_LETTER,
                    queue_name=queue_name("JOB_MINER_SCRAPE_DEAD_LETTER_QUEUE", DEAD_LETTER),
                    worker_group="recovery",
                    workload="terminal failure capture and operator review",
                ),
            )
        )
        topology.validate()
        return topology

    def validate(self) -> None:
        logical_names = tuple(lane.logical_name for lane in self.lanes)
        if logical_names != EXPECTED_LOGICAL_QUEUES:
            raise ValueError(
                "Phase 8.2 logical queue contract mismatch: "
                f"expected={EXPECTED_LOGICAL_QUEUES!r} actual={logical_names!r}"
            )

        physical_names = [lane.queue_name.strip() for lane in self.lanes]
        if any(not name for name in physical_names):
            raise ValueError("Phase 8.2 queue names cannot be empty")
        if len(set(physical_names)) != len(physical_names):
            raise ValueError(
                "Phase 8.2 physical queue names must be unique; shared queues break workload isolation"
            )

        browser_queues = {
            lane.queue_name for lane in self.lanes if lane.browser_bound
        }
        protected_queues = {
            self.queue_name(FLEET_CONTROL),
            self.queue_name(JOB_PERSISTENCE),
            self.queue_name(RECONCILIATION),
        }
        if browser_queues & protected_queues:
            raise ValueError("Browser queues cannot overlap control or state queues")

    def lane(self, logical_name: str) -> ScrapeQueueLane:
        for lane in self.lanes:
            if lane.logical_name == logical_name:
                return lane
        raise KeyError(f"Unknown Phase 8.2 logical queue: {logical_name}")

    def queue_name(self, logical_name: str) -> str:
        return self.lane(logical_name).queue_name

    def queue_names(self) -> tuple[str, ...]:
        return tuple(lane.queue_name for lane in self.lanes)

    def task_routes(self) -> dict[str, dict[str, str]]:
        """Return routes for current tasks and the stage modules that follow.

        Existing admin-portal tasks remain backward compatible. The full
        scrape task is intentionally placed on the browser/detail worker, never
        on the control or state workers. Phase 8.3 can add retry and dead-letter
        task implementations without changing physical queue names.
        """

        return {
            "src.tasks.portal_tasks.schedule_due_job_portals_task": {
                "queue": self.queue_name(FLEET_CONTROL)
            },
            "src.tasks.portal_tasks.probe_job_portal_task": {
                "queue": self.queue_name(SOURCE_DISCOVERY)
            },
            "src.tasks.portal_tasks.test_scrape_job_portal_task": {
                "queue": self.queue_name(JOB_DETAIL)
            },
            "src.tasks.portal_tasks.scrape_job_portal_task": {
                "queue": self.queue_name(JOB_DETAIL)
            },
            "src.tasks.portal_persistence_tasks.*": {
                "queue": self.queue_name(JOB_PERSISTENCE)
            },
            "src.tasks.portal_reconciliation_tasks.*": {
                "queue": self.queue_name(RECONCILIATION)
            },
            "src.tasks.portal_retry_tasks.retry_*": {
                "queue": self.queue_name(RETRY)
            },
            "src.tasks.portal_retry_tasks.dead_letter_*": {
                "queue": self.queue_name(DEAD_LETTER)
            },
        }

    def worker_groups(self) -> dict[str, tuple[str, ...]]:
        groups: dict[str, list[str]] = {}
        for lane in self.lanes:
            groups.setdefault(lane.worker_group, []).append(lane.queue_name)
        return {name: tuple(queues) for name, queues in groups.items()}

    def windows_worker_commands(self) -> tuple[str, ...]:
        commands: list[str] = []
        for group, queue_names in self.worker_groups().items():
            commands.append(
                "celery -A src.infrastructure.celery_app:celery_app worker "
                f"-Q {','.join(queue_names)} --pool=solo --concurrency=1 "
                f"--prefetch-multiplier=1 --loglevel=INFO -n phase8_2_{group}@%h"
            )
        return tuple(commands)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "logical_queue_count": len(self.lanes),
            "lanes": [lane.to_dict() for lane in self.lanes],
            "task_routes": self.task_routes(),
            "worker_groups": {
                group: list(queue_names)
                for group, queue_names in self.worker_groups().items()
            },
            "windows_worker_commands": list(self.windows_worker_commands()),
            "safety": {
                "browser_isolated_from_control": True,
                "browser_isolated_from_persistence": True,
                "browser_isolated_from_reconciliation": True,
                "scheduler_enabled": False,
                "network": False,
                "mongodb_reads": False,
                "mongodb_writes": False,
            },
        }
