#!/usr/bin/env python3
"""Seed the Goa, Bollywood, Breaking Bad, and GOT feed test data.

The script is safe to run repeatedly: each user/topic/image combination is
identified by a stable marker in its caption and is created only once. It
creates four local topic accounts when needed, then gives every existing user
three or four topic posts and writes the normal ``post.created`` outbox events
for Neo4j projection.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import hash_password
from app.database import SessionLocal
from app.graph_outbox import enqueue_graph_event
from app.hashtags import extract_hashtags
from app.models import Post, User


SEED_PASSWORD = "SeedPass123!"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".jpge"}
TOPICS = (
    ("goa", "seed_goa", "Goa moments #goa"),
    ("bollywood", "seed_bollywood", "Bollywood moments #bollywood"),
    ("bb", "seed_breaking_bad", "Breaking Bad moments #bb"),
    ("got", "seed_game_of_thrones", "Game of Thrones moments #got"),
)


def _local_only_guard() -> None:
    from app.config import APP_ENV, DB_HOST

    if APP_ENV in {"production", "prod"}:
        raise RuntimeError("refusing to seed topic data while APP_ENV is production")
    if DB_HOST not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("refusing to seed a non-local database")


def _topic_images(upload_dir: Path, topic: str) -> list[Path]:
    return sorted(
        path
        for path in upload_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
        and path.stem.lower().startswith(topic)
    )


def _get_or_create_user(db, username: str, topic: str) -> User:
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        user = User(
            username=username,
            email=f"{username}@example.test",
            password_hash=hash_password(SEED_PASSWORD),
            full_name=f"Seed {topic.upper()} account",
        )
        db.add(user)
        db.flush()
        enqueue_graph_event(
            db,
            "user.created",
            {"user_id": user.id, "username": user.username},
        )
    return user


def seed_topic_data(per_user: int = 4) -> tuple[int, int, list[str]]:
    if per_user not in {3, 4}:
        raise ValueError("per_user must be 3 or 4")
    _local_only_guard()
    upload_dir = PROJECT_ROOT / "uploads"
    if not upload_dir.exists():
        raise RuntimeError("uploads/ directory not found")

    db = SessionLocal()
    created = 0
    skipped = 0
    accounts: list[str] = []
    try:
        for topic, username, caption_prefix in TOPICS:
            _get_or_create_user(db, username, topic)

        topic_images = {
            topic: _topic_images(upload_dir, topic)
            for topic, _, _ in TOPICS
        }
        for topic, images in topic_images.items():
            if not images:
                raise RuntimeError(f"no images found for topic {topic!r} in uploads/")

        users = db.query(User).order_by(User.id.asc()).all()
        accounts = [user.username for user in users]
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for user in users:
            for slot in range(per_user):
                topic, _, caption_prefix = TOPICS[slot % len(TOPICS)]
                image = topic_images[topic][user.id % len(topic_images[topic])]
                relative_path = image.relative_to(upload_dir).as_posix()
                marker = f"[TOPIC_SEED_V2:{user.id}:{topic}:{relative_path}]"
                caption = f"{marker} {caption_prefix}"
                existing = db.query(Post).filter(Post.caption.startswith(marker)).one_or_none()
                if existing is not None:
                    skipped += 1
                    continue

                post = Post(
                    caption=caption,
                    image_path=relative_path,
                    user_id=user.id,
                    created_at=now - timedelta(seconds=created),
                )
                db.add(post)
                db.flush()
                enqueue_graph_event(
                    db,
                    "post.created",
                    {
                        "post_id": post.id,
                        "user_id": post.user_id,
                        "caption": post.caption,
                        "image_path": post.image_path,
                        "created_at": post.created_at.isoformat(),
                        "hashtags": extract_hashtags(post.caption),
                    },
                )
                created += 1

        db.commit()
        return created, skipped, accounts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-local",
        action="store_true",
        help="required safety confirmation for the local PostgreSQL database",
    )
    parser.add_argument(
        "--per-user",
        type=int,
        choices=(3, 4),
        default=4,
        help="number of topic posts to create for every existing user (default: 4)",
    )
    args = parser.parse_args()
    if not args.confirm_local:
        parser.error("pass --confirm-local; this script is intended for local demo data")

    created, skipped, accounts = seed_topic_data(args.per_user)
    print(f"Created topic posts: {created}")
    print(f"Already seeded/skipped: {skipped}")
    print(f"Users receiving topic posts: {len(accounts)}")
    print(f"Accounts: {', '.join(accounts)}")
    print(f"Seed account password: {SEED_PASSWORD}")
    print("Run the graph outbox worker to project the events into Neo4j.")


if __name__ == "__main__":
    main()
