#!/usr/bin/env python3
"""Create non-destructive local demo data for testing the personalized feed.

This script only creates records whose usernames/captions are marked as demo
data. It never deletes or modifies existing application data and never calls
Neo4j directly; the graph outbox worker handles projection separately.
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
from app.models import Follow, Like, Post, User


DEMO_PASSWORD = "DemoPass123!"
TOPICS = ("dogs", "travel", "cricket", "food", "nature", "technology", "music")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIEWER_USERNAME = "demo_user"


def _local_only_guard() -> None:
    from app.config import APP_ENV, DB_HOST

    if APP_ENV in {"production", "prod"}:
        raise RuntimeError("refusing to seed demo data while APP_ENV is production")
    if DB_HOST not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("refusing to seed a non-local database")


def _demo_users(db, count: int) -> list[User]:
    users: list[User] = []
    for index in range(count):
        username = VIEWER_USERNAME if index == 0 else f"demo_user_{index:02d}"
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(
                username=username,
                email=f"{username}@example.test",
                password_hash=hash_password(DEMO_PASSWORD),
                full_name=f"Demo {username.replace('_', ' ').title()}",
            )
            db.add(user)
            db.flush()
        users.append(user)
    return users


def _image_names() -> list[str]:
    upload_dir = Path("uploads")
    names = sorted(
        path.name for path in upload_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not names:
        raise RuntimeError("no local images found in uploads/")
    return names


def _seed_posts(db, users: list[User], target: int, images: list[str]) -> list[Post]:
    posts: list[Post] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for index in range(target):
        marker = f"[DEMO_SEED_{index:04d}]"
        post = db.query(Post).filter(Post.caption.like(f"{marker}%")).one_or_none()
        if post is None:
            topic = TOPICS[index % len(TOPICS)]
            author = users[1 + (index % (len(users) - 1))]
            post = Post(
                caption=(
                    f"{marker} Demo post about {topic} #{topic} "
                    f"#demo{index % 10}"
                ),
                image_path=images[index % len(images)],
                user_id=author.id,
                created_at=now - timedelta(minutes=index),
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
        posts.append(post)
    return posts


def _seed_follows(db, users: list[User]) -> int:
    # The demo viewer follows only three accounts so graph recommendations are
    # visible alongside second-degree and hashtag-based candidates.
    pairs = set()
    for followed in users[1:4]:
        pairs.add((users[0].id, followed.id))
    for index, user in enumerate(users[1:], start=1):
        pairs.add((user.id, users[1 + (index % (len(users) - 1))].id))

    existing = {
        (row.follower_id, row.followed_id)
        for row in db.query(Follow).filter(Follow.follower_id.in_([pair[0] for pair in pairs])).all()
    }
    created = 0
    for follower_id, followed_id in sorted(pairs):
        if follower_id == followed_id or (follower_id, followed_id) in existing:
            continue
        db.add(Follow(follower_id=follower_id, followed_id=followed_id))
        enqueue_graph_event(
            db,
            "follow.created",
            {"follower_id": follower_id, "followed_id": followed_id},
        )
        created += 1
    return created


def _seed_likes(db, users: list[User], posts: list[Post]) -> int:
    viewer = users[0]
    dog_posts = [post for post in posts if "#dogs" in (post.caption or "")]
    like_pairs = {(viewer.id, post.id) for post in dog_posts[:12]}

    # A second user shares the viewer's dog activity, creating a collaborative
    # signal for additional dog posts through Neo4j.
    similar_user = users[4] if len(users) > 4 else users[1]
    like_pairs.update((similar_user.id, post.id) for post in dog_posts[:12])
    for index, user in enumerate(users[1:], start=1):
        topic_posts = [post for post in posts if f"#{TOPICS[index % len(TOPICS)]}" in (post.caption or "")]
        like_pairs.update((user.id, post.id) for post in topic_posts[:4])

    existing = {
        (row.user_id, row.post_id)
        for row in db.query(Like).filter(Like.user_id.in_([pair[0] for pair in like_pairs])).all()
    }
    created = 0
    for user_id, post_id in sorted(like_pairs):
        if (user_id, post_id) in existing:
            continue
        db.add(Like(user_id=user_id, post_id=post_id))
        enqueue_graph_event(db, "like.created", {"user_id": user_id, "post_id": post_id})
        created += 1
    return created


def seed_demo_data(posts: int, users: int) -> tuple[int, int, int]:
    if users < 5:
        raise ValueError("--users must be at least 5")
    _local_only_guard()
    db = SessionLocal()
    try:
        demo_users = _demo_users(db, users)
        images = _image_names()
        demo_posts = _seed_posts(db, demo_users, posts, images)
        follows = _seed_follows(db, demo_users)
        likes = _seed_likes(db, demo_users, demo_posts)
        db.commit()
        return len(demo_users), len(demo_posts), follows + likes
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posts", type=int, default=350)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument(
        "--confirm-local",
        action="store_true",
        help="required safety confirmation for the local PostgreSQL database",
    )
    args = parser.parse_args()
    if not args.confirm_local:
        parser.error("pass --confirm-local; this script is intended for local demo data")
    if not 1 <= args.posts <= 1000:
        parser.error("--posts must be between 1 and 1000")
    if not 5 <= args.users <= 100:
        parser.error("--users must be between 5 and 100")

    user_count, post_count, relation_count = seed_demo_data(args.posts, args.users)
    print(f"Created or reused demo users: {user_count}")
    print(f"Created or reused demo posts: {post_count}")
    print(f"Created demo follow/like relationships: {relation_count}")
    print("Demo login: demo_user / DemoPass123!")
    print("Run the graph outbox worker to project the events into Neo4j.")


if __name__ == "__main__":
    main()
