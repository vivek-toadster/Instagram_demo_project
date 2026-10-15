from datetime import timedelta
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.graph_outbox import (
    DEAD_LETTER,
    FAILED,
    PROCESSING,
    PROCESSED,
    enqueue_graph_event,
    process_graph_outbox,
    requeue_dead_letter_events,
    utcnow,
    validate_event_payload,
)
from app.hashtags import extract_hashtags, hashtags_for_image
from app.models import GraphOutboxEvent
from app.neo4j_graph import apply_graph_event


@pytest.fixture
def outbox_store():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db, factory
    finally:
        db.close()
        engine.dispose()


def follow_payload():
    return {"follower_id": 1, "followed_id": 2}


def test_business_rollback_rolls_back_outbox_event(outbox_store):
    db, _ = outbox_store
    enqueue_graph_event(db, "follow.created", follow_payload())

    db.rollback()

    assert db.query(GraphOutboxEvent).count() == 0


def test_invalid_payload_is_rejected():
    with pytest.raises(ValueError, match="invalid payload fields"):
        validate_event_payload("follow.created", {"follower_id": 1})


def test_caption_hashtags_are_normalized_and_bounded():
    assert extract_hashtags("Dogs #Dogs #travel #travel_time") == [
        "dogs",
        "travel",
        "travel_time",
    ]


def test_image_filename_produces_topic_hashtags():
    assert hashtags_for_image("actor1.jpeg") == ["actor"]
    assert hashtags_for_image("gym7.jpeg") == ["gym", "fitness"]
    assert hashtags_for_image("fruits4.avif") == ["fruits", "food"]


def test_legacy_post_event_without_hashtags_remains_valid():
    payload = {
        "post_id": 1,
        "user_id": 2,
        "caption": "hello",
        "image_path": "photo.jpg",
        "created_at": None,
    }

    assert validate_event_payload("post.created", payload) == payload


def test_graph_failure_is_retried_with_backoff(outbox_store):
    db, factory = outbox_store
    enqueue_graph_event(db, "follow.created", follow_payload())
    db.commit()

    def fail(event_type, payload):
        raise RuntimeError("neo4j down")

    first = process_graph_outbox(factory, graph_handler=fail)
    failed = db.query(GraphOutboxEvent).one()
    assert first["failed"] == 1
    assert failed.status == FAILED
    assert failed.retry_count == 1
    assert failed.next_attempt_at > failed.created_at

    def succeed(event_type, payload):
        return None

    second = process_graph_outbox(
        factory,
        graph_handler=succeed,
        now=failed.next_attempt_at + timedelta(seconds=1),
    )
    db.expire_all()
    assert second["processed"] == 1
    assert db.query(GraphOutboxEvent).one().status == PROCESSED


def test_duplicate_events_are_safe_for_idempotent_handler(outbox_store):
    db, factory = outbox_store
    enqueue_graph_event(db, "follow.created", follow_payload())
    enqueue_graph_event(db, "follow.created", follow_payload())
    db.commit()
    relationships = set()

    def idempotent_handler(event_type, payload):
        relationships.add((payload["follower_id"], payload["followed_id"]))

    result = process_graph_outbox(factory, graph_handler=idempotent_handler)

    assert result["processed"] == 2
    assert relationships == {(1, 2)}


def test_stale_processing_event_is_recovered_after_restart(outbox_store):
    db, factory = outbox_store
    event = enqueue_graph_event(db, "follow.created", follow_payload())
    db.flush()
    event.status = PROCESSING
    event.updated_at = utcnow() - timedelta(minutes=10)
    db.commit()
    processed = []

    result = process_graph_outbox(
        factory,
        graph_handler=lambda event_type, payload: processed.append(event_type),
    )

    assert result["processed"] == 1
    assert processed == ["follow.created"]


def test_max_attempts_moves_event_to_dead_letter(outbox_store):
    db, factory = outbox_store
    enqueue_graph_event(db, "follow.created", follow_payload())
    db.commit()

    def fail(event_type, payload):
        raise RuntimeError("neo4j down")

    for _ in range(3):
        event = db.query(GraphOutboxEvent).one()
        process_graph_outbox(
            factory,
            graph_handler=fail,
            now=event.next_attempt_at + timedelta(seconds=1),
            max_attempts=3,
        )
        db.expire_all()

    event = db.query(GraphOutboxEvent).one()
    assert event.status == DEAD_LETTER
    assert event.retry_count == 3


def test_dead_letter_events_can_be_explicitly_replayed(outbox_store):
    db, factory = outbox_store
    event = enqueue_graph_event(db, "follow.created", follow_payload())
    event.status = DEAD_LETTER
    db.commit()

    assert requeue_dead_letter_events(factory) == 1
    db.expire_all()
    event = db.query(GraphOutboxEvent).one()
    assert event.status == "pending"
    assert event.last_error is None


def test_unfollow_event_uses_idempotent_relationship_delete(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.neo4j_graph._execute",
        lambda statement, **payload: captured.update(statement=statement, payload=payload),
    )

    apply_graph_event("follow.deleted", follow_payload())

    assert "DELETE r" in captured["statement"]
    assert captured["payload"] == follow_payload()


def test_post_event_projects_hashtag_relationships(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.neo4j_graph._execute",
        lambda statement, **payload: captured.update(statement=statement, payload=payload),
    )

    apply_graph_event(
        "post.created",
        {
            "post_id": 10,
            "user_id": 2,
            "caption": "#dogs",
            "image_path": "photo.jpg",
            "created_at": None,
            "hashtags": ["dogs"],
        },
    )

    assert "HAS_TAG" in captured["statement"]
    assert captured["payload"]["hashtags"] == ["dogs"]


def test_two_workers_do_not_claim_the_same_event(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outbox.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    enqueue_graph_event(db, "follow.created", follow_payload())
    db.commit()
    claimed = threading.Event()
    release = threading.Event()
    first_result = {}

    def blocking_handler(event_type, payload):
        claimed.set()
        release.wait(timeout=5)

    def first_worker():
        first_result.update(process_graph_outbox(factory, graph_handler=blocking_handler))

    worker = threading.Thread(target=first_worker)
    worker.start()
    assert claimed.wait(timeout=5)
    second_result = process_graph_outbox(factory, graph_handler=lambda event_type, payload: None)
    release.set()
    worker.join(timeout=5)
    db.close()
    engine.dispose()

    assert second_result["claimed"] == 0
    assert first_result["processed"] == 1
