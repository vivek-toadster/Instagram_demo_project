from types import SimpleNamespace

from app import health
from app.health import Readiness
from app.observability import increment, prometheus_text


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement):
        return None


def test_readiness_reports_each_dependency(monkeypatch):
    monkeypatch.setattr(health, "engine", SimpleNamespace(connect=lambda: _Connection()))
    monkeypatch.setattr(health, "neo4j_status", lambda: (False, "not exposed"))
    health.reset_cache()

    result = health.check_readiness(force=True)

    assert result == Readiness(postgres=True, neo4j=False)
    health.reset_cache()


def test_readiness_cache_avoids_repeating_dependency_checks(monkeypatch):
    calls = {"postgres": 0, "neo4j": 0}

    class Connection(_Connection):
        def __enter__(self):
            calls["postgres"] += 1
            return self

    monkeypatch.setattr(health, "engine", SimpleNamespace(connect=lambda: Connection()))

    def status():
        calls["neo4j"] += 1
        return True, None

    monkeypatch.setattr(health, "neo4j_status", status)
    monkeypatch.setenv("HEALTH_CHECK_CACHE_SECONDS", "60")
    health.reset_cache()

    assert health.check_readiness().ready
    assert health.check_readiness().ready
    assert calls == {"postgres": 1, "neo4j": 1}
    health.reset_cache()


def test_metrics_are_safe_process_local_counters():
    increment("operations_test_total")

    output = prometheus_text()

    assert "operations_test_total 1" in output
