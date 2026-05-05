from __future__ import annotations

from .detail_button_capture import DetailButtonCaptureAdapter


class JobDivaAdapter(DetailButtonCaptureAdapter):
    """JobDiva-specialized adapter built on top of DetailButtonCaptureAdapter.

    Keep JobDiva-specific selectors and pagination JS in blueprints/overrides/jobdiva.yaml.
    The shared behavior remains: click Details -> capture final URL -> go back -> paginate.
    """
