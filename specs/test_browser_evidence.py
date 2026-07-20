from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.crawl.browser_evidence import (
    BrowserEvidenceCollector,
    BrowserEvidenceOptions,
    sanitize_json_value,
)
from src.crawl.dom_snapshot import (
    DOM_SNAPSHOT_SCRIPT,
    frame_snapshot_from_payload,
)
from src.schemas import BrowserSettings


def node_payload(token: str, *, clickable: bool = False, href: str | None = None) -> dict:
    return {
        "node_token": token,
        "parent_token": None,
        "depth": 0,
        "tag": "button" if clickable else "main",
        "role": "button" if clickable else "main",
        "text": "Platform Engineer" if clickable else None,
        "href": href,
        "attributes": {"data-job-id": "REQ-7"} if clickable else {},
        "clickable": clickable,
        "visible": True,
        "in_shadow_tree": clickable,
        "shadow_host": False,
        "child_element_count": 0,
        "structural_signature": "button|button|c|" if clickable else "main|main||",
        "rect": {"x": 1, "y": 2, "width": 300, "height": 60},
    }


class FakeRequest:
    method = "POST"
    resource_type = "xhr"


class FakeResponse:
    def __init__(
        self,
        *,
        url: str = "https://careers.example.com/api/jobs",
        payload: dict | None = None,
        declared_length: int | None = None,
    ) -> None:
        self.url = url
        self.status = 200
        self.request = FakeRequest()
        self.payload = payload or {"jobs": [{"id": "7", "title": "Engineer"}]}
        self.declared_length = declared_length
        self.body_calls = 0

    async def all_headers(self):
        headers = {"content-type": "application/json"}
        if self.declared_length is not None:
            headers["content-length"] = str(self.declared_length)
        return headers

    async def body(self):
        self.body_calls += 1
        return json.dumps(self.payload).encode("utf-8")


class FakeFrame:
    def __init__(self, url: str, payload: dict | None = None) -> None:
        self.url = url
        self.payload = payload or {
            "url": url,
            "title": "Careers",
            "htmlLength": 1000,
            "nodes": [node_payload("f0:n1"), node_payload("f0:n2", clickable=True)],
            "inlineJson": [],
            "truncated": False,
        }

    async def evaluate(self, script, options):
        return self.payload


class FakePage:
    def __init__(self, frames: list[FakeFrame], responses: list[FakeResponse] | None = None) -> None:
        self.url = frames[0].url
        self.frames = frames
        self.responses = responses or []
        self.handlers = []

    def on(self, event, callback):
        if event == "response":
            self.handlers.append(callback)

    def off(self, event, callback):
        if callback in self.handlers:
            self.handlers.remove(callback)

    async def goto(self, url, **kwargs):
        self.url = url
        for response in self.responses:
            for callback in list(self.handlers):
                callback(response)
        return SimpleNamespace(status=200)

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, milliseconds):
        return None

    async def title(self):
        return "Careers"


class DomSnapshotContractTests(unittest.TestCase):
    def test_snapshot_uses_live_tokens_and_open_shadow_roots_without_xpath(self) -> None:
        self.assertIn("window.__jobMinerNodeMap", DOM_SNAPSHOT_SCRIPT)
        self.assertIn("element.shadowRoot", DOM_SNAPSHOT_SCRIPT)
        self.assertNotIn("XPath", DOM_SNAPSHOT_SCRIPT)
        self.assertNotIn("document.evaluate", DOM_SNAPSHOT_SCRIPT)

    def test_inline_json_is_sanitized_and_raw_text_is_not_persisted(self) -> None:
        raw = json.dumps(
            {
                "jobs": [{"id": "1", "title": "Engineer"}],
                "access_token": "secret-value",
            }
        )
        snapshot = frame_snapshot_from_payload(
            {
                "url": "https://careers.example.com/jobs",
                "nodes": [node_payload("f0:n1")],
                "inlineJson": [
                    {
                        "script_id": "__NEXT_DATA__",
                        "script_type": "application/json",
                        "text_length": len(raw),
                        "truncated": False,
                        "raw_sample": raw,
                    }
                ],
            },
            frame_id="f0",
            frame_url="https://careers.example.com/jobs",
            json_sanitizer=sanitize_json_value,
        )

        evidence = snapshot.inline_json[0]
        self.assertEqual(evidence.payload["jobs"][0]["id"], "1")
        self.assertNotIn("access_token", evidence.payload)
        self.assertNotIn("secret-value", snapshot.model_dump_json())

    def test_limits_reject_unbounded_configuration(self) -> None:
        with self.assertRaises(ValueError):
            BrowserEvidenceOptions(max_nodes_total=99)
        with self.assertRaises(ValueError):
            BrowserEvidenceOptions(max_network_body_bytes=100)


class BrowserEvidenceCollectorTests(unittest.IsolatedAsyncioTestCase):
    def collector(self, **updates) -> BrowserEvidenceCollector:
        options = BrowserEvidenceOptions(
            settle_time_ms=0,
            network_idle_timeout_ms=0,
            **updates,
        )
        return BrowserEvidenceCollector(BrowserSettings(), options=options)

    async def test_capture_records_linkless_shadow_node_and_sanitized_network_json(self) -> None:
        response = FakeResponse(
            payload={
                "jobs": [{"id": "7", "title": "Platform Engineer"}],
                "sessionToken": "secret-value",
            }
        )
        page = FakePage(
            [FakeFrame("https://careers.example.com/jobs")],
            responses=[response],
        )
        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            report = await self.collector().capture_page(
                page,
                "https://careers.example.com/jobs",
                allowed_hosts=("careers.example.com",),
            )

        self.assertTrue(report.success)
        self.assertEqual(report.node_count, 2)
        self.assertEqual(report.linkless_clickable_count, 1)
        self.assertTrue(report.frames[0].nodes[1].in_shadow_tree)
        self.assertEqual(len(report.network_json), 1)
        self.assertNotIn("sessionToken", report.network_json[0].payload)
        self.assertNotIn("secret-value", report.model_dump_json())

    async def test_unapproved_frame_and_network_host_are_recorded_without_body_capture(self) -> None:
        external_response = FakeResponse(url="https://telemetry.example.net/events")
        page = FakePage(
            [
                FakeFrame("https://careers.example.com/jobs"),
                FakeFrame("https://external.example.net/embedded/jobs"),
            ],
            responses=[external_response],
        )
        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            report = await self.collector().capture_page(
                page,
                "https://careers.example.com/jobs",
                allowed_hosts=("careers.example.com",),
            )

        self.assertTrue(report.success)
        self.assertEqual(len(report.frames), 2)
        self.assertIn("unapproved_frame", report.frames[1].error or "")
        self.assertEqual(report.network_json, [])
        self.assertEqual(external_response.body_calls, 0)

    async def test_declared_oversized_json_is_not_copied_into_python_memory(self) -> None:
        response = FakeResponse(declared_length=10_000)
        with patch("src.portals.safety._validate_host_is_public", return_value=None):
            captured = await self.collector(max_network_body_bytes=1_024)._capture_network_response(
                response,
                sequence=1,
                allowed_hosts=("careers.example.com",),
            )

        assert captured is not None
        _, evidence = captured
        self.assertIn("exceeded", evidence.skipped_reason or "")
        self.assertEqual(response.body_calls, 0)


if __name__ == "__main__":
    unittest.main()
