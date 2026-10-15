#!/usr/bin/env python3
"""Import every image in uploads/ as one post assigned across existing users."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal
from app.graph_outbox import enqueue_graph_event
from app.hashtags import extract_hashtags, hashtags_for_image
from app.models import Post, User


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".jpge",
}


def _local_only_guard() -> None:
    from app.config import APP_ENV, DB_HOST

    if APP_ENV in {"production", "prod"}:
        raise RuntimeError("refusing to import images while APP_ENV is production")
    if DB_HOST not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("refusing to import into a non-local database")


def _image_paths(upload_dir: Path) -> list[Path]:
    return sorted(
        path for path in upload_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def import_uploads(limit: int | None = None) -> tuple[int, int]:
    _local_only_guard()
    upload_dir = PROJECT_ROOT / "uploads"
    paths = _image_paths(upload_dir)
    if limit is not None:
        paths = paths[:limit]
    if not paths:
        raise RuntimeError("no supported images found in uploads/")

    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id.asc()).all()
        if not users:
            raise RuntimeError("create at least one user before importing images")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        created = 0
        skipped = 0
        for index, path in enumerate(paths):
            relative_path = path.relative_to(upload_dir).as_posix()
            marker = f"[UPLOAD_IMPORT:{relative_path}]"
            hashtags = hashtags_for_image(relative_path)
            caption = f"{marker} {' '.join(f'#{tag}' for tag in hashtags)}"
            existing = db.query(Post).filter(Post.caption.startswith(marker)).one_or_none()
            if existing is not None:
                if existing.caption == caption:
                    skipped += 1
                    continue
                existing.caption = caption
                post = existing
            else:
                post = Post(
                    caption=caption,
                    image_path=relative_path,
                    user_id=users[index % len(users)].id,
                    created_at=now - timedelta(seconds=index),
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
                    "hashtags": extract_hashtags(caption),
                },
            )
            created += 1
        db.commit()
        return created, skipped
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="import only the first N images")
    parser.add_argument(
        "--confirm-local",
        action="store_true",
        help="required safety confirmation for the local PostgreSQL database",
    )
    args = parser.parse_args()
    if not args.confirm_local:
        parser.error("pass --confirm-local; this importer is intended for local data")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")

    created, skipped = import_uploads(args.limit)
    print(f"Images converted to posts: {created}")
    print(f"Already imported/skipped: {skipped}")
    print("Posts were assigned round-robin across existing users.")
    print("Run the graph outbox worker to project them into Neo4j.")


if __name__ == "__main__":
    main()
