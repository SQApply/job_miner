from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crawl.browser_evidence import BrowserEvidenceCollector, BrowserEvidenceOptions
from src.schemas import BrowserSettings


class FakeRequest:
    method = "POST"
    resource_type = "xhr"


class FakeResponse:
    url = "https://careers.example.com/api/search"
    status = 200
    request = FakeRequest()

    async def all_headers(self):
        return {"content-type": "application/json", "content-length": "142"}

    async def body(self):
        return json.dumps(
            {
                "jobs": [{"id": "REQ-1", "title": "Data Engineer"}],
                "sessionToken": "must-not-be-persisted",
            }
        ).encode("utf-8")


class FakeFrame:
    url = "https://careers.example.com/jobs"

    async def evaluate(self, script, options):
        return {
            "url": self.url,
            "title": "Example Careers",
            "htmlLength": 5000,
            "nodes": [
                {
                    "node_token": "f0:n1",
                    "parent_token": None,
                    "depth": 0,
                    "tag": "main",
                    "role": "main",
                    "text": None,
                    "href": None,
                    "attributes": {},
                    "clickable": False,
                    "visible": True,
                    "in_shadow_tree": False,
                    "shadow_host": False,
                    "child_element_count": 1,
                    "structural_signature": "main|main||article",
                    "rect": {"x": 0, "y": 0, "width": 900, "height": 700},
                },
                {
                    "node_token": "f0:n2",
                    "parent_token": "f0:n1",
                    "depth": 1,
                    "tag": "button",
                    "role": "button",
                    "text": "Data Engineer",
                    "href": None,
                    "attributes": {"data-job-id": "REQ-1"},
                    "clickable": True,
                    "visible": True,
                    "in_shadow_tree": True,
                    "shadow_host": False,
                    "child_element_count": 0,
                    "structural_signature": "button|button|c|",
                    "rect": {"x": 20, "y": 30, "width": 400, "height": 80},
                },
            ],
            "inlineJson": [
                {
                    "script_id": "__NEXT_DATA__",
                    "script_type": "application/json",
                    "text_length": 100,
                    "truncated": False,
                    "raw_sample": json.dumps(
                        {
                            "jobs": [{"id": "REQ-1", "title": "Data Engineer"}],
                            "access_token": "must-not-be-persisted",
                        }
                    ),
                }
            ],
            "truncated": False,
        }


class FakePage:
    def __init__(self):
        self.url = "https://careers.example.com/jobs"
        self.frames = [FakeFrame()]
        self.handlers = []

    def on(self, event, callback):
        if event == "response":
            self.handlers.append(callback)

    def off(self, event, callback):
        if callback in self.handlers:
            self.handlers.remove(callback)

    async def goto(self, url, **kwargs):
        self.url = url
        for callback in list(self.handlers):
            callback(FakeResponse())
        return SimpleNamespace(status=200)

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, milliseconds):
        return None

    async def title(self):
        return "Example Careers"


async def smoke() -> None:
    collector = BrowserEvidenceCollector(
        BrowserSettings(),
        options=BrowserEvidenceOptions(settle_time_ms=0, network_idle_timeout_ms=0),
    )
    with patch("src.portals.safety._validate_host_is_public", return_value=None):
        report = await collector.capture_page(
            FakePage(),
            "https://careers.example.com/jobs",
            allowed_hosts=("careers.example.com",),
        )

    assert report.success
    assert report.node_count == 2
    assert report.linkless_clickable_count == 1
    assert len(report.network_json) == 1
    assert "sessionToken" not in report.network_json[0].payload
    inline_payload = report.frames[0].inline_json[0].payload
    assert "access_token" not in inline_payload
    print(
        "PHASE_7B_BROWSER_EVIDENCE_SMOKE_OK",
        json.dumps(
            {
                "frames": len(report.frames),
                "nodes": report.node_count,
                "linkless_clickables": report.linkless_clickable_count,
                "network_json": len(report.network_json),
                "secrets_persisted": 0,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    asyncio.run(smoke())
