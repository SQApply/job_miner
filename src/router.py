from __future__ import annotations

from .adapters.detail_button_capture import DetailButtonCaptureAdapter
from .adapters.generic_listing import GenericListingAdapter
from .adapters.hash_route_spa import HashRouteSpaAdapter
from .adapters.infinite_scroll import InfiniteScrollAdapter
from .adapters.jobdiva import JobDivaAdapter
from .adapters.load_more_button import LoadMoreButtonAdapter
from .adapters.modal_detail import ModalDetailAdapter
from .adapters.paginated_anchor import PaginatedAnchorAdapter
from .adapters.paginated_url_param import PaginatedUrlParamAdapter
from .adapters.search_first import SearchFirstAdapter
from .adapters.workday import WorkdayAdapter
from .schemas import ResolvedBlueprint


def get_adapter(blueprint: ResolvedBlueprint):
    adapter = blueprint.adapter

    if adapter in {"load_more", "load_more_button"}:
        return LoadMoreButtonAdapter()

    if adapter in {"paginated", "paginated_anchor"}:
        return PaginatedAnchorAdapter()

    if adapter == "paginated_url_param":
        return PaginatedUrlParamAdapter()

    if adapter == "detail_button_capture":
        return DetailButtonCaptureAdapter()

    if adapter == "infinite_scroll":
        return InfiniteScrollAdapter()

    if adapter == "generic_listing":
        return GenericListingAdapter()

    if adapter == "hash_route_spa":
        return HashRouteSpaAdapter()

    if adapter == "jobdiva":
        return JobDivaAdapter()

    if adapter == "modal_detail":
        return ModalDetailAdapter()

    if adapter == "search_first":
        return SearchFirstAdapter()

    if adapter == "workday":
        return WorkdayAdapter()

    raise ValueError(f"Unsupported adapter: {adapter}")
