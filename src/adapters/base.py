from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    @abstractmethod
    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        raise NotImplementedError