from pathlib import Path

from src.control.portal_repository import _schedule_to_interval_minutes
from src.portals.artifacts import save_portal_artifact
from src.portals.lifecycle import reconciliation_guard


def test_schedule_expression_to_minutes():
    assert _schedule_to_interval_minutes("hourly", None) == 60
    assert _schedule_to_interval_minutes("every_6h", None) == 360
    assert _schedule_to_interval_minutes("every 2 days", None) == 2880
    assert _schedule_to_interval_minutes(None, 120) == 120


def test_portal_artifact_written(tmp_path: Path):
    artifact = save_portal_artifact(
        root=tmp_path,
        portal_id="portal-1",
        run_session_id="run-1",
        category="portal_crawl",
        artifact_type="debug_json",
        name="sample",
        content={"ok": True},
        mime_type="application/json",
        extension=".json",
    )
    assert artifact
    assert artifact["storage_mode"] == "temporary_local"
    assert (tmp_path / artifact["relative_path"]).exists()
    assert artifact["size_bytes"] > 0


def test_unknown_or_partial_discovery_cannot_deactivate_catalog_jobs():
    browser_fallback = reconciliation_guard(
        discovered_urls=["https://example.com/jobs/1"],
        acquisition={"selected": False, "strategy": "browser_fallback"},
    )
    bounded_provider = reconciliation_guard(
        discovered_urls=["https://example.com/jobs/1"],
        acquisition={"selected": True, "reconciliation_safe": False},
    )

    assert browser_fallback is not None
    assert bounded_provider is not None
    assert browser_fallback["status"] == "skipped_incomplete_acquisition"
    assert bounded_provider["status"] == "skipped_incomplete_acquisition"
    assert browser_fallback["missing_marked"] == 0
    assert bounded_provider["deactivated"] == 0
