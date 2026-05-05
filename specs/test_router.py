from pathlib import Path

from src.blueprint_hub import BlueprintHub
from src.router import get_adapter


def test_router_load_more_button():
    hub = BlueprintHub(Path(__file__).resolve().parents[1])
    target = hub.get_target("strategic_staff_jobs")
    adapter = get_adapter(target)
    assert adapter.__class__.__name__ == "LoadMoreButtonAdapter"


def test_router_paginated_url_param():
    hub = BlueprintHub(Path(__file__).resolve().parents[1])
    target = hub.get_target("experis_jobs")
    adapter = get_adapter(target)
    assert adapter.__class__.__name__ == "PaginatedUrlParamAdapter"


def test_router_detail_button_capture():
    hub = BlueprintHub(Path(__file__).resolve().parents[1])
    target = hub.get_target("jobdiva_jobs")
    adapter = get_adapter(target)
    assert adapter.__class__.__name__ == "DetailButtonCaptureAdapter"
