from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.certification import CertificationOptions
from src.portals.detector import detect_portal
from src.portals.page_quality import assess_page_quality, classify_crawler_failure
from src.portals.url_intelligence import assess_llm_job_grounding, rank_job_candidate_urls
from src.schemas import JobPosting


def main() -> None:
    article = detect_portal(
        listing_url="https://staffing.example/insights",
        html="<article>Healthcare Workday AMS support and Greenhouse consulting.</article>",
    )
    assert article.source_platform == "custom_listing"

    shell_html = (
        "<html><body><div id='root'></div><script src='/runtime.js'></script>"
        "<script src='/app.js'></script></body></html>"
    )
    shell = assess_page_quality(html=shell_html)
    assert not shell.blocked and shell.surface_kind == "javascript_shell"
    assert classify_crawler_failure(
        "Blocked by anti-bot protection: Structural: no <body> tag"
    ) == "javascript_shell"

    detail_url = "https://jobs.example/job/JR-4242/data-engineer"
    ranked, metrics = rank_job_candidate_urls(
        ["https://jobs.example/forms", "https://jobs.example/jobs", detail_url],
        listing_url="https://jobs.example/jobs",
    )
    assert ranked == [detail_url]
    assert not metrics["fallback_preserved"]

    page = SimpleNamespace(
        html=(
            "<h1>Senior Data Engineer</h1><p>Acme Analytics</p>"
            "<p>Requisition ID JR-4242</p><p>Responsibilities and qualifications</p>"
        )
    )
    job = JobPosting(
        title="Senior Data Engineer",
        job_url=detail_url,
        company="Acme Analytics",
        job_reference="JR-4242",
    )
    grounded, reason = assess_llm_job_grounding(job, detail_url, page)
    assert grounded, reason
    invented, _ = assess_llm_job_grounding(
        job.model_copy(update={"company": "Imaginary Corporation"}),
        detail_url,
        page,
    )
    assert not invented
    assert not CertificationOptions().allow_llm_fallback

    print(
        "PHASE_5_5A_FLEET_TRUTH_SMOKE_OK "
        "keyword_ats_rejected=true javascript_shell_repairable=true "
        f"ranked_jobs={len(ranked)} low_confidence_fallback=false "
        "llm_grounded=true llm_invention_rejected=true certification_llm_default=off"
    )


if __name__ == "__main__":
    main()
