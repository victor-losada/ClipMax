"""Logging a archivo rotativo + un buffer en memoria que muestra la interfaz web."""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingBufferHandler(logging.Handler):
    """Guarda las últimas N líneas de log para el panel de estado."""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)
        self._lock_rb = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - un log roto nunca debe tumbar la app
            line = record.getMessage()
        with self._lock_rb:
            self.records.append(line)

    def tail(self, n: int = 100) -> list[str]:
        with self._lock_rb:
            return list(self.records)[-n:]


RING = RingBufferHandler()


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(_FORMAT, datefmt="%H:%M:%S")

    file_handler = RotatingFileHandler(
        log_dir / "clipmax.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    # En Windows la consola puede no ser UTF-8; evitamos que un emoji tumbe el log.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    RING.setFormatter(fmt)
    root.addHandler(RING)

    # Librerías ruidosas.
    for noisy in ("werkzeug", "waitress", "urllib3", "httpx", "httpx2", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
