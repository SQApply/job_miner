from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class BrowserSettings(BaseModel):
    headless: bool = True
    verbose: bool = False
    remove_overlay_elements: bool = True
    remove_consent_popups: bool = True
    use_persistent_context: bool = False
    user_data_dir: str | None = None
    scan_full_page: bool = False
    max_scroll_steps: int = 20
    scroll_delay: float = 0.25
    delay_before_return_html: float = 1.0
    listing_wait_for_timeout: int = 45000
    detail_wait_for_timeout: int = 15000
    # Generic location permission handling for Crawl4AI / Playwright
    geolocation_enabled: bool = True
    geolocation_latitude: float = 39.8283
    geolocation_longitude: float = -98.5795
    geolocation_accuracy: float = 50000.0
    #parallel extraction limit
    detail_extraction_concurrency: int = 5


class LLMSettings(BaseModel):
    provider: str
    base_url: str
    api_token: str = "ollama"
    temperature: float = 0.0
    max_tokens: int = 1400
    chunk_token_threshold: int = 1400
    overlap_rate: float = 0.05


class OutputSettings(BaseModel):
    dir: str = "data/processed"


class SystemConfig(BaseModel):
    browser: BrowserSettings
    llm: LLMSettings
    output: OutputSettings


class LoadMoreConfig(BaseModel):
    enabled: bool = False
    max_clicks: int = 0
    click_js: str = ""
    wait_for_js: str = ""


class PaginationConfig(BaseModel):
    enabled: bool = False
    max_pages: int = 500
    type: str = "numbered"  # numbered | next_button
    max_turns: int = 250
    stop_after_stable_rounds: int = 3
    next_text_patterns: list[str] = Field(default_factory=lambda: ["»", ">", "next"])
    click_js: str = ""
    wait_for_js: str = ""

    # optional site-specific override JS
    click_js_override: Optional[str] = None
    wait_for_js_override: Optional[str] = None

class ListingConfig(BaseModel):
    page_url: str = ""
    item_href_contains: str = ""
    detail_text_patterns: list[str] = Field(default_factory=lambda: ["details", "view details","learn more", "view job"])
    exclude_exact_urls: list[str] = Field(default_factory=list)
    initial_wait_for: Optional[str] = None
    session_id: str = "job_miner_session"
    load_more: LoadMoreConfig = Field(default_factory=LoadMoreConfig)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)

    # detail capture modes for paginated sites
    detail_capture_mode: str = "direct_links"   # direct_links | click_buttons
    detail_wait_for: Optional[str] = None
    detail_click_js_template: Optional[str] = None
    back_to_listing_js: Optional[str] = None
    back_to_listing_wait_for: Optional[str] = None

class DetailConfig(BaseModel):
    wait_for: str = "css:h1"
    instruction: str


class ProfileConfig(BaseModel):
    adapter: str
    listing: ListingConfig
    detail: DetailConfig


class SiteEntry(BaseModel):
    id: str
    label: str
    profile: str
    page_url: str
    allowed_hosts: list[str]
    output_file: str
    item_href_contains: Optional[str] = None
    detail_text_patterns: list[str] = Field(default_factory=list)
    exclude_exact_urls: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None

    # optional site override file name, without .yaml
    override: Optional[str] = None

    # optional site-level values
    detail_capture_mode: Optional[str] = None
    detail_wait_for: Optional[str] = None

class SiteRegistry(BaseModel):
    sites: list[SiteEntry]


class ResolvedBlueprint(BaseModel):
    id: str
    label: str
    allowed_hosts: list[str]
    output_file: str
    adapter: str
    listing: ListingConfig
    detail: DetailConfig


class JobPosting(BaseModel):
    title: str | None = Field(default=None, description="Job title exactly as shown on the page")
    job_url: str | None = Field(default=None, description="Canonical job detail URL")
    apply_url: Optional[str] = Field(default=None, description="Apply URL if present")
    company: Optional[str] = Field(default=None, description="Hiring company if stated")
    location_text: Optional[str] = Field(default=None, description="Location text as displayed")
    employment_type: Optional[str] = Field(default=None, description="Employment type such as Contract or Full-time")
    duration: Optional[str] = Field(default=None, description="Contract duration if stated")
    compensation_text: Optional[str] = Field(default=None, description="Pay or compensation text if stated")
    summary: Optional[str] = Field(default=None, description="Short summary or overview of the role")
    responsibilities: list[str] = Field(default_factory=list, description="Key responsibilities")
    required_skills: list[str] = Field(default_factory=list, description="Required skills and experience")
    preferred_skills: list[str] = Field(default_factory=list, description="Preferred or nice-to-have skills")
    job_reference: Optional[str] = Field(default=None, description="Reference job id if present")


class RunResult(BaseModel):
    target_id: str
    output_path: str
    summary_path: str
    discovered_urls: int
    extracted_jobs: int
    total_elapsed_seconds: float
    jobs: list[JobPosting]