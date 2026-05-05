from __future__ import annotations

from .detail_button_capture import DetailButtonCaptureAdapter


class ModalDetailAdapter(DetailButtonCaptureAdapter):
    """Future adapter for listings where job details open in a modal.

    Current safe behavior inherits detail-button capture. When a site needs true modal
    extraction, extend this class to capture modal content or modal apply/detail links,
    then close the modal before moving to the next card.
    """
