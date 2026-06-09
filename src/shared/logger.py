"""Structured JSON logger for CloudWatch."""
from __future__ import annotations

import json
import sys
from typing import Any


class StructuredLogger:
    """Emits JSON-formatted log entries to stdout for CloudWatch ingestion.

    Every log entry always includes ``job_id``, ``stage``, and ``outcome``.
    Additional keyword arguments are merged into the log record.
    """

    def __init__(self, stage: str) -> None:
        self._stage = stage

    def _emit(self, level: str, job_id: str, stage: str, outcome: str, **kwargs: Any) -> None:
        record: dict[str, Any] = {
            "level": level,
            "job_id": job_id,
            "stage": stage,
            "outcome": outcome,
        }
        record.update(kwargs)
        print(json.dumps(record), file=sys.stdout, flush=True)

    def info(self, job_id: str, stage: str, outcome: str, **kwargs: Any) -> None:
        """Log an INFO-level entry."""
        self._emit("INFO", job_id, stage, outcome, **kwargs)

    def error(self, job_id: str, stage: str, outcome: str, **kwargs: Any) -> None:
        """Log an ERROR-level entry."""
        self._emit("ERROR", job_id, stage, outcome, **kwargs)

    def warning(self, job_id: str, stage: str, outcome: str, **kwargs: Any) -> None:
        """Log a WARNING-level entry."""
        self._emit("WARNING", job_id, stage, outcome, **kwargs)
