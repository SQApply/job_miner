from types import SimpleNamespace

from src.crawl.link_collector import collect_job_links


def test_collect_job_links_filters_listing_page():
    result = SimpleNamespace(links={"internal": [
        {"href": "/jobs/123"},
        {"href": "/jobs"},
        {"href": "/other"},
    ]})

    urls = collect_job_links(
        result,
        page_url="https://careers.strategicstaff.com/#/jobs",
        allowed_hosts=["careers.strategicstaff.com"],
        href_contains="/jobs/",
        exclude_exact_urls=["https://careers.strategicstaff.com/jobs"],
    )

    assert urls == ["https://careers.strategicstaff.com/jobs/123"]