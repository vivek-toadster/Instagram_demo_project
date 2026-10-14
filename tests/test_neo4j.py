from types import SimpleNamespace

import pytest

from app.neo4j_client import Neo4jClient
from app import neo4j_graph
from app.neo4j_schema import SCHEMA_STATEMENTS, initialize_schema


class FakeResult:
    def consume(self):
        return None


class FakeSession:
    def __init__(self):
        self.statements = []

    def run(self, statement):
        self.statements.append(statement)
        return FakeResult()


class ParameterSession(FakeSession):
    def run(self, statement, parameters):
        self.statements.append((statement, parameters))
        return FakeResult()


class CandidateSession:
    def __init__(self):
        self.statement = None
        self.parameters = None

    def run(self, statement, **parameters):
        self.statement = statement
        self.parameters = parameters
        return [
            {"post_id": 11, "degree": 3, "interest_score": 0.8},
            {"post_id": 12, "degree": 1, "interest_score": None},
        ]


def test_schema_initialization_runs_all_idempotent_statements():
    session = FakeSession()

    initialize_schema(session)

    assert session.statements == list(SCHEMA_STATEMENTS)
    assert all("IF NOT EXISTS" in statement for statement in session.statements)


def test_incomplete_configuration_is_rejected():
    client = Neo4jClient("neo4j://localhost:7687", "neo4j", None)

    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        client.connect()


def test_connectivity_failure_closes_driver(monkeypatch):
    fake_driver = SimpleNamespace(
        verify_connectivity=lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        close=lambda: setattr(fake_driver, "closed", True),
        closed=False,
    )
    monkeypatch.setattr("app.neo4j_client.GraphDatabase.driver", lambda *args, **kwargs: fake_driver)
    client = Neo4jClient("neo4j://localhost:7687", "neo4j", "password")

    with pytest.raises(RuntimeError, match="database unavailable"):
        client.connect()

    assert fake_driver.closed is True
    assert client.driver is None


def test_close_closes_connected_driver():
    client = Neo4jClient(None, None, None)
    fake_driver = SimpleNamespace(closed=False, close=lambda: setattr(fake_driver, "closed", True))
    client.driver = fake_driver

    client.close()

    assert fake_driver.closed is True
    assert client.driver is None


def test_graph_relationship_projection_uses_postgresql_ids(monkeypatch):
    session = ParameterSession()
    monkeypatch.setattr(neo4j_graph.neo4j_client, "session", lambda: _session_context(session))

    neo4j_graph.upsert_user(7, "alice")
    neo4j_graph.upsert_post(11, 7, "hello", "image.jpg", None)
    neo4j_graph.set_follow(7, 8, following=True)
    neo4j_graph.set_like(7, 11, liked=True)
    neo4j_graph.add_comment_relationship(7, 11)

    assert len(session.statements) == 5
    assert session.statements[0][1] == {"user_id": 7, "username": "alice"}
    assert session.statements[1][1]["post_id"] == 11
    assert "MERGE (follower)-[:FOLLOWS]->(followed)" in session.statements[2][0]
    assert "MERGE (user)-[:LIKED]->(post)" in session.statements[3][0]
    assert "MERGE (user)-[:COMMENTED_ON]->(post)" in session.statements[4][0]


def test_personalized_candidate_query_returns_interest_signals(monkeypatch):
    session = CandidateSession()
    monkeypatch.setattr(neo4j_graph.neo4j_client, "session", lambda: _session_context(session))

    candidates = neo4j_graph.get_personalized_candidate_post_ids(7, 25)

    assert candidates == [(11, 3, 0.8), (12, 1, 0.0)]
    assert session.parameters == {"user_id": 7, "limit": 25}
    assert "similar_user" in session.statement
    assert "NOT EXISTS" in session.statement


class _session_context:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc_value, traceback):
        return False
