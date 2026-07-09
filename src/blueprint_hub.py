from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .schemas import ProfileConfig, ResolvedBlueprint, SiteRegistry, SystemConfig


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


class BlueprintHub:
    def __init__(self, root: Path):
        self.root = root
        self.blueprints_dir = root / "blueprints"
        self.system = self._load_system()
        self.registry = self._load_registry()

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data or {}

    def _load_system(self) -> SystemConfig:
        config = SystemConfig.model_validate(self._load_yaml(self.blueprints_dir / "system.yaml"))

        provider = os.getenv("OLLAMA_PROVIDER")
        base_url = os.getenv("OLLAMA_BASE_URL")
        api_token = os.getenv("OLLAMA_API_TOKEN")

        if provider:
            config.llm.provider = provider
        if base_url:
            config.llm.base_url = base_url
        if api_token:
            config.llm.api_token = api_token

        return config

    def _load_registry(self) -> SiteRegistry:
        return SiteRegistry.model_validate(self._load_yaml(self.blueprints_dir / "site_registry.yaml"))

    def list_target_ids(self) -> list[str]:
        return [site.id for site in self.registry.sites]

    def _fleet_target_ids(self) -> list[str]:
        """Return target ids for launch-fleet.

        Historically ``blueprints/fleet.yaml`` listed only two targets, so
        ``python -m src --root . launch-fleet`` silently skipped the rest of
        the registered websites.  ``__all__`` makes the default behavior match
        production expectations: run every active static target in
        ``site_registry.yaml``.
        """
        fleet = self._load_yaml(self.blueprints_dir / "fleet.yaml")
        raw_targets = [str(item).strip() for item in (fleet.get("targets") or []) if str(item).strip()]

        if not raw_targets or any(item.lower() in {"__all__", "all", "*"} for item in raw_targets):
            return self.list_target_ids()

        known = set(self.list_target_ids())
        unknown = [item for item in raw_targets if item not in known]
        if unknown:
            raise KeyError(f"Unknown fleet target id(s): {', '.join(unknown)}")

        return raw_targets

    def get_target(self, target_id: str) -> ResolvedBlueprint:
        site = next((item for item in self.registry.sites if item.id == target_id), None)
        if site is None:
            raise KeyError(f"Unknown target id: {target_id}")

        profile_path = self.blueprints_dir / "profiles" / f"{site.profile}.yaml"
        profile_raw = self._load_yaml(profile_path)
        profile = ProfileConfig.model_validate(profile_raw)

        resolved_raw: dict[str, Any] = {
            "id": site.id,
            "label": site.label,
            "allowed_hosts": site.allowed_hosts,
            "output_file": site.output_file,
            "adapter": profile.adapter,
            "listing": profile.listing.model_dump(),
            "detail": profile.detail.model_dump(),
        }

        resolved_raw["listing"].update(
            {
                "page_url": site.page_url,
                "item_href_contains": site.item_href_contains if site.item_href_contains is not None else profile.listing.item_href_contains,
                "detail_text_patterns": site.detail_text_patterns or profile.listing.detail_text_patterns,
                "exclude_exact_urls": site.exclude_exact_urls or profile.listing.exclude_exact_urls,
                "session_id": site.session_id or f"{site.id}_session",
                "detail_capture_mode": site.detail_capture_mode or profile.listing.detail_capture_mode,
                "detail_wait_for": site.detail_wait_for or profile.listing.detail_wait_for,
            }
        )

        if site.override:
            override_path = self.blueprints_dir / "overrides" / f"{site.override}.yaml"
            override_raw = self._load_yaml(override_path)
            resolved_raw = _deep_merge(resolved_raw, override_raw)

        return ResolvedBlueprint.model_validate(resolved_raw)

    def get_fleet_targets(self) -> list[ResolvedBlueprint]:
        return [self.get_target(target_id) for target_id in self._fleet_target_ids()]
