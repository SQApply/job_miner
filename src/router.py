from __future__ import annotations

from .adapters.load_more import LoadMoreAdapter
from .adapters.paginated import PaginatedAdapter
from .schemas import ResolvedBlueprint


def get_adapter(blueprint: ResolvedBlueprint):
    if blueprint.adapter == "load_more":
        return LoadMoreAdapter()
    if blueprint.adapter == "paginated":
        return PaginatedAdapter()
    raise ValueError(f"Unsupported adapter: {blueprint.adapter}")