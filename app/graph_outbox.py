"""Transactional PostgreSQL outbox and bounded Neo4j event processor."""

from datetime import datetime, timedelta, timezone
import logging
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import GraphOutboxEvent
from app.neo4j_graph import apply_graph_event
from app.observability import increment, log_event

logger = logging.getLogger(__name__)

PENDING = "pending"
PROCESSING = "processing"
FAILED = "failed"
PROCESSED = "processed"
DEAD_LETTER = "dead_letter"
MAX_ATTEMPTS = 5
STALE_PROCESSING_SECONDS = 300

EVENT_PAYLOAD_FIELDS = {
    "user.created": {"user_id", "username"},
    "post.created": {"post_id", "user_id", "caption", "image_path", "created_at"},
    "post.deleted": {"post_id"},
    "follow.created": {"follower_id", "followed_id"},
    "follow.deleted": {"follower_id", "followed_id"},
    "like.created": {"user_id", "post_id"},
    "like.deleted": {"user_id", "post_id"},
    "comment.created": {"comment_id", "user_id", "post_id"},
    "comment.deleted": {"comment_id", "user_id", "post_id", "has_remaining_comments"},
}
OPTIONAL_EVENT_PAYLOAD_FIELDS = {"post.created": {"hashtags"}}


def utcnow() -> datetime:
    """Return a naive UTC timestamp matching the existing DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_event_payload(event_type: str, payload: object) -> dict:
    """Validate an event before it can enter or leave the outbox."""
    required = EVENT_PAYLOAD_FIELDS.get(event_type)
    if required is None:
        raise ValueError(f"unsupported graph event type: {event_type}")
    if not isinstance(payload, dict):
        raise ValueError("graph event payload must be an object")
    allowed = required | OPTIONAL_EVENT_PAYLOAD_FIELDS.get(event_type, set())
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise ValueError(f"invalid payload fields for {event_type}")
    for key, value in payload.items():
        if key.endswith("_id") and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError(f"invalid identifier field: {key}")
    if "has_remaining_comments" in payload and not isinstance(payload["has_remaining_comments"], bool):
        raise ValueError("has_remaining_comments must be boolean")
    for key in ("username", "image_path"):
        if key in payload and (not isinstance(payload[key], str) or not payload[key]):
            raise ValueError(f"{key} must be a non-empty string")
    for key in ("caption", "created_at"):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            raise ValueError(f"{key} must be a string or null")
    if "hashtags" in payload:
        hashtags = payload["hashtags"]
        if (
            not isinstance(hashtags, list)
            or len(hashtags) > 30
            or any(
                not isinstance(tag, str)
                or not 1 <= len(tag) <= 50
                or tag != tag.lower()
                or not tag.replace("_", "").isalnum()
                for tag in hashtags
            )
        ):
            raise ValueError("hashtags must be a list of normalized names")
    if not isinstance(payload.get("username", ""), str):
        raise ValueError("username must be a string")
    return payload


def enqueue_graph_event(
    db: Session,
    event_type: str,
    payload: dict,
    event_id: str | None = None,
) -> GraphOutboxEvent:
    """Add a validated graph event to the caller's current DB transaction."""
    validate_event_payload(event_type, payload)
    timestamp = utcnow()
    event = GraphOutboxEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        payload=payload,
        status=PENDING,
        retry_count=0,
        created_at=timestamp,
        updated_at=timestamp,
        next_attempt_at=timestamp,
    )
    db.add(event)
    return event


def _recover_stale_events(db: Session, now: datetime) -> None:
    """Return events abandoned by a stopped worker to the pending queue."""
    cutoff = now - timedelta(seconds=STALE_PROCESSING_SECONDS)
    db.query(GraphOutboxEvent).filter(
        GraphOutboxEvent.status == PROCESSING,
        GraphOutboxEvent.updated_at < cutoff,
    ).update(
        {
            GraphOutboxEvent.status: PENDING,
            GraphOutboxEvent.next_attempt_at: now,
            GraphOutboxEvent.updated_at: now,
        },
        synchronize_session=False,
    )


def _backoff_seconds(retry_count: int) -> int:
    """Calculate capped exponential retry delay for a failed event."""
    return min(300, 2 ** max(retry_count - 1, 0))


def process_graph_outbox(
    session_factory: Callable[[], Session],
    max_events: int = 100,
    graph_handler: Callable[[str, dict], None] = apply_graph_event,
    now: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, int]:
    """Claim and process a bounded batch; graph calls occur outside DB transactions."""
    now = now or utcnow()
    claim_db = session_factory()
    try:
        _recover_stale_events(claim_db, now)
        query = (
            claim_db.query(GraphOutboxEvent)
            .filter(
                GraphOutboxEvent.status.in_([PENDING, FAILED]),
                GraphOutboxEvent.next_attempt_at <= now,
            )
            .order_by(GraphOutboxEvent.created_at.asc(), GraphOutboxEvent.event_id.asc())
            .limit(max(1, min(max_events, 100)))
        )
        if claim_db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        events = query.all()
        for event in events:
            event.status = PROCESSING
            event.updated_at = now
        claim_db.commit()
        claimed_ids = [event.event_id for event in events]
    finally:
        claim_db.close()

    result = {"claimed": len(claimed_ids), "processed": 0, "failed": 0, "dead_letter": 0}
    increment("outbox_events_claimed_total", result["claimed"])
    for event_id in claimed_ids:
        event_db = session_factory()
        try:
            event = event_db.get(GraphOutboxEvent, event_id)
            if event is None or event.status != PROCESSING:
                continue
            try:
                payload = validate_event_payload(event.event_type, event.payload)
                graph_handler(event.event_type, payload)
            except ValueError as exc:
                event.status = DEAD_LETTER
                event.retry_count += 1
                event.last_error = str(exc)[:500]
                event.updated_at = utcnow()
                result["dead_letter"] += 1
                increment("outbox_events_dead_letter_total")
            except Exception as exc:
                event.retry_count += 1
                event.updated_at = utcnow()
                event.last_error = f"{type(exc).__name__}: graph operation failed"
                if event.retry_count >= max_attempts:
                    event.status = DEAD_LETTER
                    result["dead_letter"] += 1
                    increment("outbox_events_dead_letter_total")
                else:
                    event.status = FAILED
                    event.next_attempt_at = event.updated_at + timedelta(
                        seconds=_backoff_seconds(event.retry_count)
                    )
                    result["failed"] += 1
                    increment("outbox_events_retry_total")
                logger.exception("Graph event %s failed; status=%s; error=%s", event_id, event.status, exc)
            else:
                event.status = PROCESSED
                event.processed_at = utcnow()
                event.updated_at = event.processed_at
                event.last_error = None
                result["processed"] += 1
                increment("outbox_events_processed_total")
            event_db.commit()
        finally:
            event_db.close()
    log_event("outbox_batch", **result)
    return result


def requeue_dead_letter_events(session_factory: Callable[[], Session], limit: int = 100) -> int:
    """Explicitly make dead-lettered events eligible for a later retry."""
    db = session_factory()
    try:
        events = (
            db.query(GraphOutboxEvent)
            .filter(GraphOutboxEvent.status == DEAD_LETTER)
            .order_by(GraphOutboxEvent.created_at.asc(), GraphOutboxEvent.event_id.asc())
            .limit(max(1, min(limit, 1000)))
            .all()
        )
        now = utcnow()
        for event in events:
            event.status = PENDING
            event.next_attempt_at = now
            event.updated_at = now
            event.last_error = None
        db.commit()
        return len(events)
    finally:
        db.close()
