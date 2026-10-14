"""Small dependency-free operational metrics and structured event logging."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import json
import logging
from threading import Lock
from typing import Any


logger = logging.getLogger("instagram_clone")
_lock = Lock()
_counters: defaultdict[str, int] = defaultdict(int)
_gauges: dict[str, int] = {}


def increment(name: str, value: int = 1) -> None:
    """Increment a process-local counter; metrics reset when the process exits."""
    if value <= 0:
        return
    with _lock:
        _counters[name] += value


def set_gauge(name: str, value: int) -> None:
    with _lock:
        _gauges[name] = int(value)


def snapshot() -> tuple[dict[str, int], dict[str, int]]:
    with _lock:
        return dict(_counters), dict(_gauges)


def prometheus_text() -> str:
    counters, gauges = snapshot()
    lines = []
    for name, value in sorted({**counters, **gauges}.items()):
        safe_name = name if name.replace("_", "").isalnum() else "app_metric"
        lines.append(f"# TYPE {safe_name} counter")
        lines.append(f"{safe_name} {value}")
    return "\n".join(lines) + ("\n" if lines else "")


def log_event(event: str, **fields: Any) -> None:
    """Emit one JSON event without accepting request bodies or secret values."""
    record: dict[str, Any] = {"event": event, **fields}
    logger.info(json.dumps(record, default=str, sort_keys=True))

