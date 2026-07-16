from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.detector import detect_portal
from src.portals.result_evidence import detection_html, structured_link_evidence


def main() -> None:
    canonical_icims = (
        "https://c-13769-20240415-careers-insightglobal-com.i.icims.com/"
        "insightglobal-careers/jobs"
    )
    apex_result = SimpleNamespace(
        cleaned_html="<h1>Consultant Careers</h1>",
        links={
            "internal": [{"href": "/search-results-usa", "text": "Search Jobs"}],
            "external": [],
        },
    )
    icims_result = SimpleNamespace(
        cleaned_html="<h1>Career Portal</h1>",
        links={
            "internal": [],
            "external": [{"href": canonical_icims, "text": "Search Jobs"}],
        },
    )
    apex = detect_portal(
        listing_url="https://www.apexsystems.com/consultant-careers",
        html=detection_html(apex_result, apex_result.cleaned_html),
    )
    icims = detect_portal(
        listing_url="https://careers-insightglobal.icims.com/jobs",
        html=detection_html(icims_result, icims_result.cleaned_html),
    )
    apex_route = apex.acquisition_hints.get("listing_url")
    icims_route = icims.acquisition_hints.get("listing_url")
    if apex_route != "https://www.apexsystems.com/search-results-usa":
        raise SystemExit("Apex structured listing route was not promoted")
    if icims_route != canonical_icims:
        raise SystemExit("iCIMS structured tenant route was not promoted")
    link_count = structured_link_evidence(apex_result).count("<a ") + structured_link_evidence(icims_result).count("<a ")
    print(
        "PHASE_5_2_STRUCTURED_LINKS_SMOKE_OK",
        f"structured_links={link_count}",
        f"apex_route={apex_route}",
        f"icims_platform={icims.source_platform}",
        "gpu_llm_calls=0",
    )


if __name__ == "__main__":
    main()
