from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from ..schemas import ProfileConfig, ResolvedBlueprint
from .safety import normalize_allowed_hosts

SUPPORTED_PROFILES = {
    "generic_listing",
    "paginated_anchor",
    "paginated_url_param",
    "load_more_button",
    "infinite_scroll",
    "hash_route_spa",
    "detail_button_capture",
    "modal_detail",
    "search_first",
    "workday",
    "jobdiva",
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(base)
    for key, incoming in override.items():
        if key in value and isinstance(value[key], dict) and isinstance(incoming, dict):
            value[key] = _deep_merge(value[key], incoming)
        else:
            value[key] = incoming
    return value


def build_portal_blueprint(*, root: Path, portal: dict[str, Any], run_session_id: str) -> ResolvedBlueprint:
    profile_name = str(portal.get("profile_name") or "generic_listing").strip()
    if profile_name not in SUPPORTED_PROFILES:
        raise ValueError(f"Unsupported portal profile: {profile_name}")

    profile_path = root / "blueprints" / "profiles" / f"{profile_name}.yaml"
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile definition is missing: {profile_path}")

    source = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    configuration = portal.get("configuration_json") or {}
    overrides = configuration.get("profile_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("Portal profile_overrides must be an object.")

    resolved = _deep_merge(source, overrides)
    profile = ProfileConfig.model_validate(resolved)
    listing_payload = profile.listing.model_dump(mode="python")
    listing_payload["page_url"] = str(portal.get("canonical_listing_url") or portal.get("listing_url") or "")
    listing_payload["session_id"] = f"portal_{str(portal['id']).replace('-', '')[:16]}_{run_session_id[-12:]}"

    max_pages = int(portal.get("max_pages_per_run") or 50)
    pagination = dict(listing_payload.get("pagination") or {})
    pagination["max_turns"] = min(int(pagination.get("max_turns") or max_pages), max_pages)
    listing_payload["pagination"] = pagination

    output_file = f"portal_{str(portal['id']).replace('-', '')}.jsonl"
    return ResolvedBlueprint(
        id=str(portal["target_id"]),
        label=str(portal.get("display_name") or portal["target_id"]),
        allowed_hosts=list(normalize_allowed_hosts(portal.get("allowed_hosts") or [])),
        output_file=output_file,
        adapter=profile.adapter,
        listing=profile.listing.__class__.model_validate(listing_payload),
        detail=profile.detail,
    )
