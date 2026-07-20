from __future__ import annotations

from abc import ABC, abstractmethod

from ..portals.contracts import DiscoveryBatch, ScrapeStrategy


class BaseAdapter(ABC):
    async def discover_candidates(
        self,
        crawler,
        blueprint,
        system_config,
        session_logger=None,
    ) -> DiscoveryBatch:
        """Project a legacy URL adapter into the Phase 7 candidate contract.

        Existing adapters do not need to change.  Candidate-native adapters may
        override this method when they can discover linkless cards, API records,
        modals, inline details, or frame-backed jobs.
        """

        urls = await self.discover_job_urls(
            crawler,
            blueprint,
            system_config,
            session_logger=session_logger,
        )
        return DiscoveryBatch.from_urls(
            list(urls or []),
            strategy=ScrapeStrategy.BLUEPRINT_DOM,
            metrics={"adapter_mode": "legacy_url_projection"},
        )

    @abstractmethod
    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        raise NotImplementedError