"""Idempotent Neo4j schema statements for the graph projection."""

SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT user_id_unique IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE",
    "CREATE CONSTRAINT post_id_unique IF NOT EXISTS FOR (p:Post) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT category_name_unique IF NOT EXISTS FOR (c:Category) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT hashtag_name_unique IF NOT EXISTS FOR (h:Hashtag) REQUIRE h.name IS UNIQUE",
    "CREATE INDEX user_username_index IF NOT EXISTS FOR (u:User) ON (u.username)",
    "CREATE INDEX post_user_id_index IF NOT EXISTS FOR (p:Post) ON (p.user_id)",
    "CREATE INDEX post_created_at_index IF NOT EXISTS FOR (p:Post) ON (p.created_at)",
)


def initialize_schema(session) -> None:
    """Create graph constraints and indexes if they do not already exist."""
    for statement in SCHEMA_STATEMENTS:
        session.run(statement).consume()
