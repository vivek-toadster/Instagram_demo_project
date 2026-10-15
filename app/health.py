"""Cached dependency checks used by readiness probes."""

from __future__ import annotations

from dataclasses import dataclass
import os
from threading import Lock
import time

from sqlalchemy import text

from app.database import engine
from app.neo4j_client import neo4j_status
from app.observability import increment


@dataclass(frozen=True)
class Readiness:
    postgres: bool
    neo4j: bool

    @property
    def ready(self) -> bool:
        """Return whether both PostgreSQL and Neo4j are currently reachable."""
        return self.postgres and self.neo4j


_lock = Lock()
_cached: tuple[float, Readiness] | None = None


def _cache_seconds() -> float:
    """Read the readiness cache duration from the environment safely."""
    try:
        return max(0.0, float(os.getenv("HEALTH_CHECK_CACHE_SECONDS", "5")))
    except ValueError:
        return 5.0


def check_readiness(force: bool = False) -> Readiness:
    """Check both dependencies, using a short cache unless forced."""
    global _cached
    now = time.monotonic()
    with _lock:
        if not force and _cached and now - _cached[0] < _cache_seconds():
            return _cached[1]

    postgres = True
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        postgres = False
        increment("postgres_connectivity_failures_total")

    neo4j, _error = neo4j_status()
    if not neo4j:
        increment("neo4j_connectivity_failures_total")

    result = Readiness(postgres=postgres, neo4j=neo4j)
    with _lock:
        _cached = (time.monotonic(), result)
    return result


def reset_cache() -> None:
    """Clear the cached readiness result, primarily for tests and recovery."""
    global _cached
    with _lock:
        _cached = None
