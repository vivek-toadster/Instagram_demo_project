from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import feed
from app.database import Base
from app.models import Comment, Like, Post, User


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def add_user(db, user_id: int, username: str) -> User:
    user = User(
        id=user_id,
        username=username,
        email=f"{username}@example.com",
        password_hash="not-used",
    )
    db.add(user)
    db.flush()
    return user


def add_post(db, post_id: int, user_id: int, created_at: datetime) -> Post:
    post = Post(id=post_id, user_id=user_id, image_path=f"{post_id}.jpg", created_at=created_at)
    db.add(post)
    db.flush()
    return post


def test_direct_and_second_degree_candidates_are_ranked_by_relationship(monkeypatch, db):
    now = datetime.now(timezone.utc)
    author = add_user(db, 2, "direct")
    friend_of_friend = add_user(db, 3, "fof")
    direct_post = add_post(db, 11, author.id, now - timedelta(hours=2))
    fof_post = add_post(db, 12, friend_of_friend.id, now - timedelta(minutes=1))
    db.commit()
    monkeypatch.setattr(feed, "get_candidate_post_ids", lambda user_id, limit: [(12, 2), (11, 1)])

    posts = feed.get_feed_posts(db, user_id=1, page_size=20)

    assert [post.id for post in posts] == [11, 12]


def test_recency_increases_score():
    now = datetime.now(timezone.utc)
    old = Post(id=1, created_at=now - timedelta(days=5))
    new = Post(id=2, created_at=now - timedelta(minutes=1))
    config = feed.FeedRankingConfig()

    assert feed.score_row(feed.FeedRow(new, 0, 0, 1), config, now) > feed.score_row(
        feed.FeedRow(old, 0, 0, 1), config, now
    )


def test_engagement_increases_score():
    now = datetime.now(timezone.utc)
    post = Post(id=1, created_at=now)
    config = feed.FeedRankingConfig()

    assert feed.score_row(feed.FeedRow(post, 20, 10, 1), config, now) > feed.score_row(
        feed.FeedRow(post, 0, 0, 1), config, now
    )


def test_duplicate_candidates_keep_direct_connection():
    assert feed.deduplicate_candidates([(8, 2), (8, 1), (9, 2), (8, 1)]) == [(8, 1), (9, 2)]


def test_personalized_candidates_keep_strongest_interest_signal():
    assert feed.deduplicate_personalized_candidates(
        [(8, 3, 0.2), (8, 3, 0.8), (8, 2, 0.1)]
    ) == [(8, 2, 0.1)]


def test_interest_signal_affects_ranking(db, monkeypatch):
    now = datetime.now(timezone.utc)
    add_user(db, 2, "first")
    add_user(db, 3, "second")
    add_post(db, 11, 2, now)
    add_post(db, 12, 3, now)
    db.commit()
    monkeypatch.setattr(
        feed,
        "get_candidate_post_ids",
        lambda user_id, limit: [(11, 2, 1.0), (12, 2, 0.0)],
    )

    posts = feed.get_feed_posts(db, user_id=1, page_size=20)

    assert [post.id for post in posts] == [11, 12]


def test_personalized_feed_excludes_already_liked_posts(db, monkeypatch):
    now = datetime.now(timezone.utc)
    add_user(db, 1, "viewer")
    add_user(db, 2, "author")
    add_post(db, 11, 2, now)
    add_post(db, 12, 2, now)
    db.add(Like(user_id=1, post_id=11))
    db.commit()
    monkeypatch.setattr(
        feed,
        "get_candidate_post_ids",
        lambda user_id, limit: [(11, 1, 1.0), (12, 1, 0.5)],
    )

    posts = feed.get_feed_posts(db, user_id=1, page_size=20)

    assert [post.id for post in posts] == [12]


def test_empty_graph_falls_back_to_postgres(db, monkeypatch):
    now = datetime.now(timezone.utc)
    add_user(db, 2, "author")
    add_post(db, 5, 2, now)
    db.commit()
    monkeypatch.setattr(feed, "get_candidate_post_ids", lambda user_id, limit: [])

    assert [post.id for post in feed.get_feed_posts(db, user_id=1)] == [5]


def test_stale_graph_ids_are_ignored(db, monkeypatch):
    now = datetime.now(timezone.utc)
    add_user(db, 2, "author")
    add_post(db, 5, 2, now)
    db.commit()
    monkeypatch.setattr(feed, "get_candidate_post_ids", lambda user_id, limit: [(999999, 1), (5, 1)])

    assert [post.id for post in feed.get_feed_posts(db, user_id=1)] == [5]


def test_neo4j_failure_uses_postgres_fallback(db, monkeypatch):
    now = datetime.now(timezone.utc)
    add_user(db, 2, "author")
    add_post(db, 5, 2, now)
    db.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(feed, "get_candidate_post_ids", fail)

    assert [post.id for post in feed.get_feed_posts(db, user_id=1)] == [5]


def test_pagination_is_deterministic(db, monkeypatch):
    now = datetime.now(timezone.utc)
    add_user(db, 2, "author")
    for post_id in (1, 2, 3):
        add_post(db, post_id, 2, now)
    db.commit()
    monkeypatch.setattr(feed, "get_candidate_post_ids", lambda user_id, limit: [(1, 1), (2, 1), (3, 1)])

    assert [post.id for post in feed.get_feed_posts(db, user_id=1, page=1, page_size=2)] == [3, 2]
    assert [post.id for post in feed.get_feed_posts(db, user_id=1, page=2, page_size=2)] == [1]


def test_page_size_and_candidate_limit_are_bounded(db, monkeypatch):
    seen = {}

    def candidates(user_id, limit):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(feed, "get_candidate_post_ids", candidates)

    assert feed.get_feed_posts(db, user_id=1, page=999, page_size=10000) == []
    assert seen["limit"] == 1000


def test_equal_scores_use_post_id_tie_breaker():
    now = datetime.now(timezone.utc)
    rows = [
        feed.FeedRow(Post(id=4, created_at=now), 0, 0, 1),
        feed.FeedRow(Post(id=9, created_at=now), 0, 0, 1),
    ]

    assert [row.post.id for row in feed.rank_rows(rows, feed.FeedRankingConfig(), now)] == [9, 4]


def test_feed_requires_authentication(monkeypatch):
    from app import routes

    request = Request({"type": "http", "method": "GET", "path": "/feed", "headers": []})
    monkeypatch.setattr(routes, "get_current_user", lambda request, db: None)

    response = routes.feed(request, page=1, limit=20, db=object())

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
