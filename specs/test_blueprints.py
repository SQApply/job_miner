from pathlib import Path

from src.blueprint_hub import BlueprintHub


def test_blueprints_load():
    hub = BlueprintHub(Path(__file__).resolve().parents[1])
    target = hub.get_target("strategic_staff_jobs")
    assert target.adapter == "load_more_button"
    assert target.listing.page_url
    assert target.detail.instruction
