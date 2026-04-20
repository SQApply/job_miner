from __future__ import annotations

from ..schemas import LoadMoreConfig


def should_continue(load_more: LoadMoreConfig, click_index: int) -> bool:
    return load_more.enabled and click_index < load_more.max_clicks