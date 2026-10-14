"""Reusable Neo4j driver lifecycle and session management."""

from contextlib import contextmanager
import logging
from collections.abc import Iterator

from neo4j import Driver, GraphDatabase

from app.config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME

logger = logging.getLogger(__name__)


class Neo4jClient:
    """Own the process-wide Neo4j driver and its application lifecycle."""

    def __init__(self, uri: str | None, username: str | None, password: str | None, database: str | None = None):
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database
        self.driver: Driver | None = None

    @property
    def configured(self) -> bool:
        return bool(self.uri and self.username and self.password)

    def connect(self) -> None:
        if not self.configured:
            raise RuntimeError("Neo4j configuration is incomplete; set NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD")
        self.driver = GraphDatabase.driver(
            self.uri,
            auth=(self.username, self.password),
            connection_timeout=5.0,
        )
        try:
            self.driver.verify_connectivity()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.driver is not None:
            self.driver.close()
            self.driver = None

    @contextmanager
    def session(self) -> Iterator:
        if self.driver is None:
            raise RuntimeError("Neo4j driver is not connected")
        session_kwargs = {"database": self.database} if self.database else {}
        with self.driver.session(**session_kwargs) as session:
            yield session

    def verify_connectivity(self) -> None:
        if self.driver is None:
            raise RuntimeError("Neo4j driver is not connected")
        self.driver.verify_connectivity()


neo4j_client = Neo4jClient(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE)


def initialize_neo4j() -> None:
    """Connect and initialize schema; callers decide whether failure is fatal."""
    from app.neo4j_schema import initialize_schema

    neo4j_client.connect()
    with neo4j_client.session() as session:
        initialize_schema(session)


def shutdown_neo4j() -> None:
    neo4j_client.close()


def neo4j_status() -> tuple[bool, str | None]:
    try:
        neo4j_client.verify_connectivity()
    except Exception as exc:
        logger.debug("Neo4j connectivity check failed", exc_info=True)
        return False, str(exc)
    return True, None
