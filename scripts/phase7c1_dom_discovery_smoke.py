from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.crawl.browser_evidence import BrowserEvidenceReport
from src.crawl.dom_snapshot import DomNodeEvidence, FrameDomSnapshot
from src.portals.dom_discovery import DomCandidateDiscoverer, preserve_evidence_backed_urls


def node(
    token: str,
    parent: str | None,
    tag: str,
    signature: str,
    *,
    text: str | None = None,
    href: str | None = None,
    role: str | None = None,
    clickable: bool = False,
) -> DomNodeEvidence:
    return DomNodeEvidence(
        node_token=token,
        parent_token=parent,
        depth=token.count(":"),
        tag=tag,
        role=role,
        text=text,
        href=href,
        clickable=clickable,
        structural_signature=signature,
    )


def report() -> BrowserEvidenceReport:
    nodes = [
        node("n:body", None, "body", "body|||main"),
        node("n:main", "n:body", "main", "main|main||table", role="main"),
        node("n:table", "n:main", "table", "table|||tbody"),
        node("n:tbody", "n:table", "tbody", "tbody|||tr,tr,tr"),
    ]
    for index, title in enumerate(("Cloud Engineer", "Data Analyst", "QA Lead"), start=1):
        row = f"n:r{index}"
        url = f"https://careers.example.com/opening?record={index}"
        nodes.append(node(row, "n:tbody", "tr", "tr|||td,td,td"))
        for field, value in enumerate((title, f"City {index}", "Remote"), start=1):
            cell = f"{row}:c{field}"
            nodes.append(node(cell, row, "td", "td|||a"))
            nodes.append(
                node(
                    f"{cell}:a",
                    cell,
                    "a",
                    "a|link|c|",
                    text=value,
                    href=url,
                    role="link",
                    clickable=True,
                )
            )
    now = datetime.now(timezone.utc).isoformat()
    return BrowserEvidenceReport(
        requested_url="https://careers.example.com/careers",
        final_url="https://careers.example.com/careers",
        success=True,
        status_code=200,
        title="Open Jobs",
        started_at=now,
        completed_at=now,
        frames=[
            FrameDomSnapshot(
                frame_id="f0",
                frame_url="https://careers.example.com/careers",
                nodes=nodes,
            )
        ],
    )


batch = DomCandidateDiscoverer().discover(report())
assert len(batch.candidates) == 3, batch.model_dump(mode="json")
assert [candidate.title_hint for candidate in batch.candidates] == [
    "Cloud Engineer",
    "Data Analyst",
    "QA Lead",
]
preserved, metrics = preserve_evidence_backed_urls([], batch.candidates)
assert preserved == batch.discovered_urls
assert metrics["adaptive_urls_preserved"] == 3
print(
    "PHASE_7C1_DOM_DISCOVERY_SMOKE_OK",
    json.dumps(
        {
            "candidates": len(batch.candidates),
            "clusters": batch.metrics["clusters_qualified"],
            "preserved_unfamiliar_urls": metrics["adaptive_urls_preserved"],
        },
        sort_keys=True,
    ),
)
