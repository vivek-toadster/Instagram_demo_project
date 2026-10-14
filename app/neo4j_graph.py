"""Best-effort projection of PostgreSQL entities and relationships into Neo4j."""

import logging
from collections.abc import Callable

from app.hashtags import extract_hashtags
from app.neo4j_client import neo4j_client

logger = logging.getLogger(__name__)

CANDIDATE_POST_QUERY = """
CALL () {
    MATCH (viewer:User {id: $user_id})-[:FOLLOWS]->(author:User)-[:CREATED]->(post:Post)
    RETURN post.id AS post_id, 1 AS degree
    UNION
    MATCH (viewer:User {id: $user_id})-[:FOLLOWS]->(:User)-[:FOLLOWS]->(author:User)-[:CREATED]->(post:Post)
    WHERE author.id <> $user_id
    RETURN post.id AS post_id, 2 AS degree
}
WITH post_id, min(degree) AS degree
RETURN post_id, degree
ORDER BY degree ASC, post_id DESC
LIMIT $limit
"""

PERSONALIZED_CANDIDATE_POST_QUERY = """
CALL () {
    MATCH (viewer:User {id: $user_id})-[:FOLLOWS]->(:User)-[:CREATED]->(post:Post)
    WHERE post.user_id <> $user_id
      AND NOT EXISTS { MATCH (viewer)-[:LIKED]->(post) }
    RETURN post.id AS post_id, 1 AS degree, 0.0 AS interest_score
    UNION
    MATCH (viewer:User {id: $user_id})-[:FOLLOWS]->(:User)-[:FOLLOWS]->(:User)-[:CREATED]->(post:Post)
    WHERE post.user_id <> $user_id
      AND NOT EXISTS { MATCH (viewer)-[:LIKED]->(post) }
    RETURN post.id AS post_id, 2 AS degree, 0.0 AS interest_score
    UNION
    MATCH (viewer:User {id: $user_id})-[:LIKED]->(liked_post:Post)
          <-[:LIKED]-(similar_user:User)-[:LIKED]->(post:Post)
    WHERE similar_user.id <> $user_id
      AND post.user_id <> $user_id
      AND NOT EXISTS { MATCH (viewer)-[:LIKED]->(post) }
    RETURN post.id AS post_id, 3 AS degree,
           CASE WHEN count(DISTINCT liked_post) > 10
                THEN 1.0
                ELSE count(DISTINCT liked_post) / 10.0
           END AS interest_score
    UNION
    MATCH (viewer:User {id: $user_id})-[:LIKED]->(:Post)-[:HAS_TAG]->(tag:Hashtag)
          <-[:HAS_TAG]-(post:Post)
    WHERE post.user_id <> $user_id
      AND NOT EXISTS { MATCH (viewer)-[:LIKED]->(post) }
    RETURN post.id AS post_id, 3 AS degree,
           CASE WHEN count(DISTINCT tag) > 5
                THEN 1.0
                ELSE count(DISTINCT tag) / 5.0
           END AS interest_score
}
WITH post_id, min(degree) AS degree, max(interest_score) AS interest_score
RETURN post_id, degree, interest_score
ORDER BY interest_score DESC, degree ASC, post_id DESC
LIMIT $limit
"""


def _run(operation: Callable, *args, **kwargs) -> None:
    """Run a graph projection without making PostgreSQL transactions fail."""
    try:
        operation(*args, **kwargs)
    except Exception as exc:
        logger.warning("Neo4j projection failed: %s: %s", type(exc).__name__, exc)


def _execute(statement: str, **parameters) -> None:
    with neo4j_client.session() as session:
        session.run(statement, parameters).consume()


def get_candidate_post_ids(user_id: int, limit: int) -> list[tuple[int, int]]:
    """Return bounded, de-duplicated graph candidates and their shortest degree."""
    with neo4j_client.session() as session:
        records = session.run(CANDIDATE_POST_QUERY, user_id=user_id, limit=limit)
        return [(record["post_id"], record["degree"]) for record in records]


def get_personalized_candidate_post_ids(user_id: int, limit: int) -> list[tuple[int, int, float]]:
    """Return bounded candidates with relationship degree and collaborative interest."""
    with neo4j_client.session() as session:
        records = session.run(
            PERSONALIZED_CANDIDATE_POST_QUERY,
            user_id=user_id,
            limit=limit,
        )
        return [
            (
                record["post_id"],
                int(record["degree"]),
                float(record["interest_score"] or 0.0),
            )
            for record in records
        ]


def apply_graph_event(event_type: str, payload: dict) -> None:
    """Apply one validated event using idempotent graph operations."""
    statements = {
        "user.created": """
            MERGE (u:User {id: $user_id})
            SET u.username = $username
        """,
        "post.created": """
            MERGE (u:User {id: $user_id})
            MERGE (p:Post {id: $post_id})
            SET p.caption = $caption, p.image_path = $image_path,
                p.user_id = $user_id, p.created_at = $created_at
            MERGE (u)-[:CREATED]->(p)
            WITH p
            FOREACH (hashtag_name IN $hashtags |
                MERGE (h:Hashtag {name: hashtag_name})
                MERGE (p)-[:HAS_TAG]->(h)
            )
        """,
        "post.deleted": """
            MATCH (p:Post {id: $post_id})
            DETACH DELETE p
        """,
        "follow.created": """
            MERGE (follower:User {id: $follower_id})
            MERGE (followed:User {id: $followed_id})
            MERGE (follower)-[:FOLLOWS]->(followed)
        """,
        "follow.deleted": """
            MATCH (follower:User {id: $follower_id})-[r:FOLLOWS]->(followed:User {id: $followed_id})
            DELETE r
        """,
        "like.created": """
            MERGE (u:User {id: $user_id})
            MERGE (p:Post {id: $post_id})
            MERGE (u)-[:LIKED]->(p)
        """,
        "like.deleted": """
            MATCH (u:User {id: $user_id})-[r:LIKED]->(p:Post {id: $post_id})
            DELETE r
        """,
        "comment.created": """
            MERGE (u:User {id: $user_id})
            MERGE (p:Post {id: $post_id})
            MERGE (u)-[:COMMENTED_ON]->(p)
        """,
        "comment.deleted": """
            MATCH (u:User {id: $user_id})-[r:COMMENTED_ON]->(p:Post {id: $post_id})
            WHERE $has_remaining_comments = false
            DELETE r
        """,
    }
    statement = statements.get(event_type)
    if statement is None:
        raise ValueError(f"unsupported graph event type: {event_type}")
    parameters = dict(payload)
    if event_type == "post.created":
        # Events created before hashtag support remain replayable.
        parameters.setdefault("hashtags", extract_hashtags(parameters.get("caption")))
    _execute(statement, **parameters)


def upsert_user(user_id: int, username: str) -> None:
    _run(
        _execute,
        """
        MERGE (u:User {id: $user_id})
        SET u.username = $username
        """,
        user_id=user_id,
        username=username,
    )


def upsert_post(post_id: int, user_id: int, caption: str | None, image_path: str, created_at) -> None:
    _run(
        _execute,
        """
        MERGE (u:User {id: $user_id})
        MERGE (p:Post {id: $post_id})
        SET p.caption = $caption,
            p.image_path = $image_path,
            p.user_id = $user_id,
            p.created_at = $created_at
        MERGE (u)-[:CREATED]->(p)
        """,
        post_id=post_id,
        user_id=user_id,
        caption=caption,
        image_path=image_path,
        created_at=created_at.isoformat() if created_at else None,
    )


def set_follow(follower_id: int, followed_id: int, following: bool) -> None:
    statement = (
        """
        MERGE (follower:User {id: $follower_id})
        MERGE (followed:User {id: $followed_id})
        MERGE (follower)-[:FOLLOWS]->(followed)
        """
        if following
        else """
        MATCH (follower:User {id: $follower_id})-[relationship:FOLLOWS]->(followed:User {id: $followed_id})
        DELETE relationship
        """
    )
    _run(_execute, statement, follower_id=follower_id, followed_id=followed_id)


def set_like(user_id: int, post_id: int, liked: bool) -> None:
    statement = (
        """
        MERGE (user:User {id: $user_id})
        MERGE (post:Post {id: $post_id})
        MERGE (user)-[:LIKED]->(post)
        """
        if liked
        else """
        MATCH (user:User {id: $user_id})-[relationship:LIKED]->(post:Post {id: $post_id})
        DELETE relationship
        """
    )
    _run(_execute, statement, user_id=user_id, post_id=post_id)


def add_comment_relationship(user_id: int, post_id: int) -> None:
    _run(
        _execute,
        """
        MERGE (user:User {id: $user_id})
        MERGE (post:Post {id: $post_id})
        MERGE (user)-[:COMMENTED_ON]->(post)
        """,
        user_id=user_id,
        post_id=post_id,
    )


def remove_comment_relationship(user_id: int, post_id: int) -> None:
    _run(
        _execute,
        """
        MATCH (user:User {id: $user_id})-[relationship:COMMENTED_ON]->(post:Post {id: $post_id})
        DELETE relationship
        """,
        user_id=user_id,
        post_id=post_id,
    )


def delete_post(post_id: int) -> None:
    _run(
        _execute,
        """
        MATCH (post:Post {id: $post_id})
        DETACH DELETE post
        """,
        post_id=post_id,
    )
