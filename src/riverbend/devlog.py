"""Dev only: keeps the latest log lines in memory so the staff console can show them.

Attached to the root logger, so it receives the application's own logs (worker outcomes, model
calls, warnings, tracebacks). Uvicorn's per-request access lines don't propagate to the root
logger, so they stay in the terminal only.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime

_FORMAT = logging.Formatter("%(message)s")


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self._lines: deque[dict] = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        # Handler.handle already holds self.lock here.
        self._seq += 1
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + _FORMAT.formatException(record.exc_info)
        self._lines.append(
            {
                "seq": self._seq,
                "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
        )

    def lines(self, after: int = 0, limit: int = 200) -> list[dict]:
        with self.lock:
            newer = [line for line in self._lines if line["seq"] > after]
        return newer[-limit:]


BUFFER = LogBuffer()


def install() -> LogBuffer:
    """Attach the buffer to the root logger once, however many apps are created (tests make many)."""
    root = logging.getLogger()
    if BUFFER not in root.handlers:
        root.addHandler(BUFFER)
    return BUFFER
