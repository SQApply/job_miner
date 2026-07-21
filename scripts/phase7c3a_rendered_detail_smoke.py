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
from src.portals.rendered_detail import RenderedDetailExtractor


def node(
    token: str,
    parent: str | None,
    tag: str,
    *,
    text: str | None = None,
    role: str | None = None,
    href: str | None = None,
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
        structural_signature=f"{tag}|{role or ''}|{'c' if clickable else ''}|",
    )


def report(url: str, title: str, nodes: list[DomNodeEvidence]) -> BrowserEvidenceReport:
    now = datetime.now(timezone.utc).isoformat()
    return BrowserEvidenceReport(
        requested_url=url,
        final_url=url,
        success=True,
        status_code=200,
        title=title,
        started_at=now,
        completed_at=now,
        frames=[FrameDomSnapshot(frame_id="f0", frame_url=url, nodes=nodes)],
    )


def main() -> None:
    listing_url = "https://careers.example.com/jobs"
    category_nodes = [
        node("n:body", None, "body"),
        node("n:main", "n:body", "main", role="main"),
        node("n:grid", "n:main", "div"),
    ]
    for index, label in enumerate(
        ("Salary guide", "Professional services", "Permanent recruitment"),
        start=1,
    ):
        root = f"n:c{index}"
        category_nodes.extend(
            [
                node(root, "n:grid", "article", role="article"),
                node(f"{root}:h", root, "h2", text=label, role="heading"),
                node(
                    f"{root}:p",
                    root,
                    "p",
                    text="Browse workforce insights and employer services.",
                ),
                node(
                    f"{root}:a",
                    root,
                    "a",
                    text="Learn more",
                    href=f"https://careers.example.com/employers/category-{index}",
                    role="link",
                    clickable=True,
                ),
            ]
        )
    categories = DomCandidateDiscoverer().discover(
        report(listing_url, "Find Jobs and Careers", category_nodes)
    )
    preserved, preservation = preserve_evidence_backed_urls([], categories.candidates)
    assert preserved == []

    detail_url = "https://careers.example.com/opening?opaque=phase7c3a"
    detail_nodes = [
        node("n:body", None, "body"),
        node("n:main", "n:body", "main", role="main"),
        node(
            "n:title",
            "n:main",
            "h1",
            text="Cloud Reliability Engineer",
            role="heading",
        ),
        node("n:location", "n:main", "div", text="Location: Remote"),
        node("n:id", "n:main", "div", text="Job ID: CRE-31"),
        node("n:heading", "n:main", "h2", text="Responsibilities", role="heading"),
        node(
            "n:description",
            "n:main",
            "p",
            text=(
                "Job description: Build resilient cloud services, improve observability, "
                "automate deployments, and partner with engineering teams to operate secure "
                "production platforms across multiple regions."
            ),
        ),
        node(
            "n:requirements",
            "n:main",
            "p",
            text="Requirements: Python, Kubernetes, networking, and incident response experience.",
        ),
    ]
    extracted = RenderedDetailExtractor().extract(
        report(detail_url, "Cloud Reliability Engineer", detail_nodes),
        fallback_url=detail_url,
    )
    assert extracted.job is not None
    assert extracted.job.title == "Cloud Reliability Engineer"

    print(
        "PHASE_7C3A_RENDERED_DETAIL_SMOKE_OK",
        json.dumps(
            {
                "false_cluster_candidates": len(categories.candidates),
                "false_cluster_preserved": len(preserved),
                "weak_evidence_rejections": preservation[
                    "adaptive_urls_rejected_weak_evidence"
                ],
                "rendered_title": extracted.job.title,
                "rendered_reference": extracted.job.job_reference,
                "detail_signals": extracted.metrics["detail_signals"],
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
