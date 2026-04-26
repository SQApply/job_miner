from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ...schemas import OcrDocument, OcrSettings


class OcrBackend(ABC):
    def __init__(self, settings: OcrSettings, root: Path):
        self.settings = settings
        self.root = root

    @abstractmethod
    def parse(self, source_path: Path, *, resume_id: str, sha256: str) -> OcrDocument:
        raise NotImplementedError
