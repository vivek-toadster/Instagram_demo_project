"""Candidate retrieval, explainable ranking, and PostgreSQL fallback for feeds."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from sqlalchemy import exists, func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import Comment, Like, Post
from app.neo4j_graph import get_personalized_candidate_post_ids
from app.observability import increment

# Keep the existing feed module hook available for callers/tests that replace
# the graph candidate provider. New calls receive personalized triples.
get_candidate_post_ids = get_personalized_candidate_post_ids


@dataclass(frozen=True)
class FeedRankingConfig:
    """Weights and limits controlling personalized feed ranking."""
    recency_weight: float = 0.45
    social_weight: float = 0.25
    engagement_weight: float = 0.15
    interest_weight: float = 0.15
    recency_half_life_hours: float = 48.0
    candidate_limit: int = 500
    page_size: int = 20

    @classmethod
    def from_environment(cls) -> "FeedRankingConfig":
        """Build ranking settings from environment overrides with safe bounds."""
        return cls(
            recency_weight=_positive_float("FEED_RECENCY_WEIGHT", cls.recency_weight),
            social_weight=_positive_float("FEED_SOCIAL_WEIGHT", cls.social_weight),
            engagement_weight=_positive_float("FEED_ENGAGEMENT_WEIGHT", cls.engagement_weight),
            interest_weight=_positive_float("FEED_INTEREST_WEIGHT", cls.interest_weight),
            recency_half_life_hours=_positive_float(
                "FEED_RECENCY_HALF_LIFE_HOURS", cls.recency_half_life_hours
            ),
            candidate_limit=_bounded_int("FEED_CANDIDATE_LIMIT", cls.candidate_limit, 1, 1000),
            page_size=_bounded_int("FEED_PAGE_SIZE", cls.page_size, 1, 100),
        )


@dataclass(frozen=True)
class FeedRow:
    """A post plus the aggregate and graph signals used to rank it."""
    post: Post
    likes: int
    comments: int
    degree: int = 0
    interest_score: float = 0.0


def _positive_float(name: str, default: float) -> float:
    """Read a positive floating-point setting or return its default."""
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer setting and clamp it to an allowed range."""
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def normalize_weights(config: FeedRankingConfig) -> tuple[float, float, float, float]:
    """Normalize ranking weights so their combined contribution equals one."""
    total = (
        config.recency_weight
        + config.social_weight
        + config.engagement_weight
        + config.interest_weight
    )
    return (
        config.recency_weight / total,
        config.social_weight / total,
        config.engagement_weight / total,
        config.interest_weight / total,
    )


def deduplicate_candidates(candidates: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep the strongest (lowest) degree for each PostgreSQL post ID."""
    best_degree: dict[int, int] = {}
    for post_id, degree in candidates:
        if post_id is None:
            continue
        best_degree[post_id] = min(degree, best_degree.get(post_id, degree))
    return sorted(best_degree.items(), key=lambda item: (item[1], -item[0]))


def deduplicate_personalized_candidates(
    candidates: list[tuple[int, int] | tuple[int, int, float]],
) -> list[tuple[int, int, float]]:
    """Keep the strongest degree and highest interest signal per post."""
    best: dict[int, tuple[int, float]] = {}
    for candidate in candidates:
        post_id, degree = candidate[:2]
        interest_score = candidate[2] if len(candidate) > 2 else 0.0
        if post_id is None:
            continue
        current = best.get(post_id)
        candidate = (degree, max(0.0, min(float(interest_score), 1.0)))
        if current is None or candidate[0] < current[0] or (
            candidate[0] == current[0] and candidate[1] > current[1]
        ):
            best[post_id] = candidate
    return sorted(
        ((post_id, degree, interest) for post_id, (degree, interest) in best.items()),
        key=lambda item: (-item[2], item[1], -item[0]),
    )


def _post_rows_query(db: Session):
    """Build the eager-loaded query used by ranked and fallback feeds."""
    like_count = (
        db.query(func.count(Like.id))
        .filter(Like.post_id == Post.id)
        .correlate(Post)
        .scalar_subquery()
    )
    comment_count = (
        db.query(func.count(Comment.id))
        .filter(Comment.post_id == Post.id)
        .correlate(Post)
        .scalar_subquery()
    )
    return (
        db.query(
            Post,
            like_count,
            comment_count,
        )
        .options(
            joinedload(Post.author),
            selectinload(Post.likes),
            selectinload(Post.comments).selectinload(Comment.author),
        )
    )


def load_post_rows(
    db: Session,
    post_ids: list[int] | None = None,
    offset: int = 0,
    limit: int | None = None,
    exclude_liked_user_id: int | None = None,
) -> list[FeedRow]:
    """Load posts with aggregate engagement counts and optional graph IDs."""
    query = _post_rows_query(db)
    if post_ids is not None:
        if not post_ids:
            return []
        query = query.filter(Post.id.in_(post_ids))
    if exclude_liked_user_id is not None:
        query = query.filter(
            ~exists().where(
                (Like.user_id == exclude_liked_user_id) & (Like.post_id == Post.id)
            )
        )
    query = query.order_by(Post.created_at.desc(), Post.id.desc())
    if limit is not None:
        query = query.offset(offset).limit(limit)
    return [FeedRow(post, int(likes), int(comments)) for post, likes, comments in query.all()]


def _as_utc(value: datetime | None, now: datetime) -> datetime:
    """Treat database timestamps as UTC so age calculations are consistent."""
    if value is None:
        return now
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def score_row(row: FeedRow, config: FeedRankingConfig, now: datetime) -> float:
    """Calculate a weighted score from recency, relationship, engagement, and interest."""
    age_hours = max(0.0, (now - _as_utc(row.post.created_at, now)).total_seconds() / 3600)
    recency = math.exp(-math.log(2) * age_hours / config.recency_half_life_hours)
    social = 1.0 if row.degree == 1 else 0.5 if row.degree == 2 else 0.0
    engagement = min(1.0, math.log1p(row.likes + row.comments) / math.log1p(100))
    recency_weight, social_weight, engagement_weight, interest_weight = normalize_weights(config)
    interest = max(0.0, min(row.interest_score, 1.0))
    return (
        recency * recency_weight
        + social * social_weight
        + engagement * engagement_weight
        + interest * interest_weight
    )


def rank_rows(rows: list[FeedRow], config: FeedRankingConfig, now: datetime | None = None) -> list[FeedRow]:
    """Sort feed rows by score and use post ID as a deterministic tie-breaker."""
    now = now or datetime.now(timezone.utc)
    return sorted(rows, key=lambda row: (-score_row(row, config, now), -row.post.id))


def load_fallback_feed(db: Session, page: int, page_size: int) -> list[Post]:
    """Load a recent PostgreSQL-only page when graph candidates are unavailable."""
    offset = (page - 1) * page_size
    return [row.post for row in load_post_rows(db, offset=offset, limit=page_size)]


def get_feed_posts(db: Session, user_id: int, page: int = 1, page_size: int | None = None) -> list[Post]:
    """Build the personalized feed and safely fall back on any graph failure."""
    config = FeedRankingConfig.from_environment()
    page = max(page, 1)
    page_size = page_size or config.page_size
    page_size = max(1, min(page_size, 100))
    candidate_limit = min(max(config.candidate_limit, page * page_size), 1000)

    try:
        candidates = deduplicate_personalized_candidates(
            get_candidate_post_ids(user_id, candidate_limit)
        )
        if not candidates:
            increment("feed_fallback_total")
            return load_fallback_feed(db, page, page_size)
        signals_by_post_id = {
            post_id: (degree, interest)
            for post_id, degree, interest in candidates
        }
        rows = load_post_rows(
            db,
            post_ids=list(signals_by_post_id),
        )
        if not rows:
            increment("feed_fallback_total")
            return load_fallback_feed(db, page, page_size)
        ranked = rank_rows(
            [
                FeedRow(
                    row.post,
                    row.likes,
                    row.comments,
                    *signals_by_post_id.get(row.post.id, (0, 0.0)),
                )
                for row in rows
            ],
            config,
        )
        start = (page - 1) * page_size
        return [row.post for row in ranked[start : start + page_size]]
    except Exception:
        increment("feed_fallback_total")
        return load_fallback_feed(db, page, page_size)
