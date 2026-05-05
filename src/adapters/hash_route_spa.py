from __future__ import annotations

from .load_more_button import LoadMoreButtonAdapter


class HashRouteSpaAdapter(LoadMoreButtonAdapter):
    """Future-ready adapter for hash-route SPAs such as /#/jobs.

    Optimization rule:
    - Reuse LoadMoreButtonAdapter behavior first.
    - Put only hash-route specific waits and route guards in YAML.
    - Do not create site-specific code unless the route hydration logic is unique.
    """
