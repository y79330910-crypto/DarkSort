from pathlib import Path

from PySide6.QtCore import QThread, Signal

from darksort.scanning.discovery import discover
from darksort.scanning.scanner import scan
from darksort.settings import Settings


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(self, settings: Settings, cache_path: Path, *, refresh=False, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.cache_path = cache_path
        self.refresh = refresh

    def run(self) -> None:
        try:
            discovery = discover(self.settings)
            if self.isInterruptionRequested():
                return
            result = scan(
                discovery.roots,
                self.cache_path,
                excluded_paths=self.settings.excluded_paths,
                progress=self.progress.emit,
                cancelled=self.isInterruptionRequested,
                refresh=self.refresh,
            )
            result.issues[:0] = discovery.issues
            self.completed.emit(discovery, result)
        except Exception as exc:
            import logging

            logging.exception("Scan failed")
            self.failed.emit(str(exc))
