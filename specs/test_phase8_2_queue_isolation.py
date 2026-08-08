from __future__ import annotations

import unittest

from src.infrastructure.scrape_queue_topology import (
    DEAD_LETTER,
    EXPECTED_LOGICAL_QUEUES,
    FLEET_CONTROL,
    JOB_DETAIL,
    JOB_PERSISTENCE,
    RECONCILIATION,
    RETRY,
    SOURCE_DISCOVERY,
    ScrapeQueueTopology,
)


class Phase82QueueIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = ScrapeQueueTopology.from_environment({})

    def test_contract_has_the_seven_planned_queues(self) -> None:
        self.assertEqual(
            tuple(lane.logical_name for lane in self.topology.lanes),
            EXPECTED_LOGICAL_QUEUES,
        )
        self.assertEqual(self.topology.queue_names(), EXPECTED_LOGICAL_QUEUES)

    def test_browser_workers_do_not_consume_control_or_state_queues(self) -> None:
        browser_queues = {
            lane.queue_name for lane in self.topology.lanes if lane.browser_bound
        }
        self.assertEqual(browser_queues, {SOURCE_DISCOVERY, JOB_DETAIL})
        self.assertTrue(
            browser_queues.isdisjoint(
                {FLEET_CONTROL, JOB_PERSISTENCE, RECONCILIATION}
            )
        )

    def test_existing_portal_tasks_route_to_isolated_lanes(self) -> None:
        routes = self.topology.task_routes()
        self.assertEqual(
            routes["src.tasks.portal_tasks.schedule_due_job_portals_task"]["queue"],
            FLEET_CONTROL,
        )
        self.assertEqual(
            routes["src.tasks.portal_tasks.probe_job_portal_task"]["queue"],
            SOURCE_DISCOVERY,
        )
        self.assertEqual(
            routes["src.tasks.portal_tasks.scrape_job_portal_task"]["queue"],
            JOB_DETAIL,
        )

    def test_future_state_and_failure_tasks_have_reserved_routes(self) -> None:
        routes = self.topology.task_routes()
        self.assertEqual(
            routes["src.tasks.portal_persistence_tasks.*"]["queue"],
            JOB_PERSISTENCE,
        )
        self.assertEqual(
            routes["src.tasks.portal_reconciliation_tasks.*"]["queue"],
            RECONCILIATION,
        )
        self.assertEqual(
            routes["src.tasks.portal_retry_tasks.retry_*"]["queue"],
            RETRY,
        )
        self.assertEqual(
            routes["src.tasks.portal_retry_tasks.dead_letter_*"]["queue"],
            DEAD_LETTER,
        )

    def test_environment_override_is_supported_without_changing_logical_names(self) -> None:
        topology = ScrapeQueueTopology.from_environment(
            {"JOB_MINER_JOB_DETAIL_QUEUE": "job_detail_gpu_1"}
        )
        self.assertEqual(topology.queue_name(JOB_DETAIL), "job_detail_gpu_1")
        self.assertEqual(topology.lane(JOB_DETAIL).logical_name, JOB_DETAIL)

    def test_legacy_environment_keys_are_migrated(self) -> None:
        topology = ScrapeQueueTopology.from_environment(
            {
                "JOB_MINER_PORTAL_SCHEDULER_QUEUE": "legacy_control",
                "JOB_MINER_PORTAL_PROBE_QUEUE": "legacy_probe",
                "JOB_MINER_PORTAL_SCRAPE_QUEUE": "legacy_scrape",
                "JOB_MINER_PORTAL_ARTIFACT_QUEUE": "legacy_persistence",
            }
        )
        self.assertEqual(topology.queue_name(FLEET_CONTROL), "legacy_control")
        self.assertEqual(topology.queue_name(SOURCE_DISCOVERY), "legacy_probe")
        self.assertEqual(topology.queue_name(JOB_DETAIL), "legacy_scrape")
        self.assertEqual(topology.queue_name(JOB_PERSISTENCE), "legacy_persistence")

    def test_duplicate_physical_queues_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            ScrapeQueueTopology.from_environment(
                {
                    "JOB_MINER_FLEET_CONTROL_QUEUE": "shared",
                    "JOB_MINER_JOB_DETAIL_QUEUE": "shared",
                }
            )

    def test_windows_worker_groups_cover_every_queue_once(self) -> None:
        flattened = [
            queue
            for queues in self.topology.worker_groups().values()
            for queue in queues
        ]
        self.assertCountEqual(flattened, EXPECTED_LOGICAL_QUEUES)
        self.assertEqual(len(flattened), len(set(flattened)))
        commands = self.topology.windows_worker_commands()
        self.assertEqual(len(commands), len(self.topology.worker_groups()))
        self.assertTrue(all("--pool=solo" in command for command in commands))
        self.assertTrue(all("--concurrency=1" in command for command in commands))

    def test_celery_declares_all_phase8_2_queues_and_disables_typo_queues(self) -> None:
        from src.infrastructure.celery_app import (
            FLEET_CONTROL_QUEUE,
            JOB_DETAIL_QUEUE,
            PORTAL_PROBE_QUEUE,
            PORTAL_SCHEDULER_QUEUE,
            PORTAL_SCRAPE_QUEUE,
            SOURCE_DISCOVERY_QUEUE,
            celery_app,
        )

        declared = {queue.name for queue in celery_app.conf.task_queues}
        self.assertTrue(set(self.topology.queue_names()).issubset(declared))
        self.assertFalse(bool(celery_app.conf.task_create_missing_queues))
        self.assertEqual(PORTAL_PROBE_QUEUE, SOURCE_DISCOVERY_QUEUE)
        self.assertEqual(PORTAL_SCRAPE_QUEUE, JOB_DETAIL_QUEUE)
        self.assertEqual(PORTAL_SCHEDULER_QUEUE, FLEET_CONTROL_QUEUE)

        routes = celery_app.conf.task_routes
        self.assertEqual(
            routes["src.tasks.portal_tasks.schedule_due_job_portals_task"]["queue"],
            FLEET_CONTROL_QUEUE,
        )
        self.assertEqual(
            routes["src.tasks.portal_tasks.probe_job_portal_task"]["queue"],
            SOURCE_DISCOVERY_QUEUE,
        )
        self.assertEqual(
            routes["src.tasks.portal_tasks.scrape_job_portal_task"]["queue"],
            JOB_DETAIL_QUEUE,
        )


if __name__ == "__main__":
    unittest.main()
