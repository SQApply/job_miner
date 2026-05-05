from __future__ import annotations

from .paginated_url_param import PaginatedUrlParamAdapter


class WorkdayAdapter(PaginatedUrlParamAdapter):
    """Future Workday adapter.

    Workday career pages usually need either URL-param pagination or API-backed listing
    discovery. Start with this generic URL-param behavior; if Crawl4AI sees only an SPA
    shell, add a Workday-specific API discovery method here later.
    """
