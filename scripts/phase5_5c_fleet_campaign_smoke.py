from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.portals.certification import PortalInventoryEntry
from src.portals.fleet_campaign import (
    build_fleet_campaign_report,
    select_campaign_source_ids,
    write_fleet_campaign_artifacts,
)


def _entry(index: int) -> PortalInventoryEntry:
    return PortalInventoryEntry(
        source_id=f"smoke_{index:03d}",
        display_name=f"Smoke Portal {index}",
        listing_url=f"https://portal-{index}.example/jobs",
        source_row=index + 1,
    )


def _record(entry: PortalInventoryEntry, classification: str) -> dict:
    base = {
        "contract_version": "1.3",
        "run_id": "phase55c-smoke",
        "attempt_number": 1,
        "source_id": entry.source_id,
        "status": "failed",
        "certification_status": "needs_repair",
        "detected_platform": "custom_listing",
        "surface_kind": "content",
        "resolved_route_url": None,
        "discovered_urls": 0,
        "attempted_urls": 0,
        "extracted_jobs": 0,
        "error_type": "zero_discovery",
    }
    if classification == "certified":
        base.update(
            status="success",
            certification_status="passed",
            discovered_urls=10,
            attempted_urls=10,
            extracted_jobs=10,
            error_type=None,
        )
    elif classification == "protected":
        base.update(
            status="blocked",
            certification_status="access_blocked",
            surface_kind="confirmed_access_control",
            error_type="access_blocked",
        )
    elif classification == "route":
        base["resolved_route_url"] = f"https://jobs.{entry.source_id}.example/openings"
    elif classification == "javascript":
        base.update(surface_kind="javascript_shell", error_type="javascript_shell")
    elif classification == "network":
        base["error_type"] = "network_error"
    elif classification == "detail":
        base.update(discovered_urls=10, attempted_urls=10, error_type="zero_valid_jobs")
    return base


def main() -> None:
    inventory = [_entry(index) for index in range(102)]
    classes = (
        ["certified"] * 80
        + ["protected"] * 5
        + ["route"] * 4
        + ["javascript"] * 4
        + ["network"] * 3
        + ["detail"] * 3
        + ["zero"] * 3
    )
    latest = {
        entry.source_id: _record(entry, classification)
        for entry, classification in zip(inventory, classes, strict=True)
    }
    report = build_fleet_campaign_report(
        inventory=inventory,
        latest_records=latest,
        campaign_id="phase55c-smoke",
        target_successes=80,
    )
    assert report["complete"]
    assert report["target_met"]
    assert report["certified_source_count"] == 80
    assert report["blocked_source_count"] == 5
    assert report["gpu_eligible_source_count"] == 3
    retry_ids = select_campaign_source_ids(report, mode="retry-actionable")
    gpu_retry_ids = select_campaign_source_ids(report, mode="retry-gpu-eligible")
    assert len(retry_ids) == 17
    assert len(gpu_retry_ids) == 3
    assert set(gpu_retry_ids) <= set(retry_ids)
    assert not set(report["blocked_source_ids"]) & set(retry_ids)

    with tempfile.TemporaryDirectory() as directory:
        paths = write_fleet_campaign_artifacts(
            output_dir=Path(directory),
            report=report,
        )
        assert all(Path(path).exists() for path in paths.values())

    print(
        "PHASE_5_5C_FLEET_CAMPAIGN_SMOKE_OK",
        f"inventory={report['inventory_count']}",
        f"accounted={report['accounted_count']}",
        f"certified={report['certified_source_count']}",
        f"blocked={report['blocked_source_count']}",
        f"actionable={report['actionable_source_count']}",
        f"gpu_eligible={report['gpu_eligible_source_count']}",
        f"retry_sources={len(retry_ids)}",
        f"gpu_retry_sources={len(gpu_retry_ids)}",
        f"target_met={str(report['target_met']).lower()}",
    )


if __name__ == "__main__":
    main()
