"""Groq-backed assistant with a deterministic local fallback."""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone, timedelta

import httpx

from app.config import GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL, GROQ_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def build_growth_context(db, user) -> dict:
    """Build assistant facts from PostgreSQL; absent data is represented honestly."""
    from app.models import Comment, Follow, Like, Post

    posts = db.query(Post).filter(Post.user_id == user.id).order_by(Post.created_at.desc()).all()
    followers = db.query(Follow).filter(Follow.followed_id == user.id).count()
    following = db.query(Follow).filter(Follow.follower_id == user.id).count()
    post_ids = [post.id for post in posts]
    likes = db.query(Like).filter(Like.post_id.in_(post_ids)).count() if post_ids else 0
    comments = db.query(Comment).filter(Comment.post_id.in_(post_ids)).count() if post_ids else 0
    rows = []
    hashtags = Counter()
    for post in posts:
        post_likes = len(post.likes)
        post_comments = len(post.comments)
        hashtags.update(re.findall(r"#([\w-]+)", post.caption or ""))
        rows.append({"caption": (post.caption or "")[:160], "likes": post_likes, "comments": post_comments})
    rows.sort(key=lambda row: (row["likes"] + row["comments"], row["likes"]), reverse=True)
    return {
        "username": user.username,
        "followers": followers,
        "following": following,
        "posts": len(posts),
        "total_likes": likes,
        "total_comments": comments,
        "avg_likes": round(likes / len(posts), 1) if posts else None,
        "avg_comments": round(comments / len(posts), 1) if posts else None,
        "top_posts": rows[:5],
        "top_hashtags": hashtags.most_common(10),
    }


def _post_context(post, growth_context=None) -> str:
    if post is None:
        return "No specific post was selected."
    author = getattr(post.author, "username", "another user")
    selected = f"Selected post author: @{author}\nSelected post caption: {post.caption or 'No caption was added.'}"
    return selected + (f"\nCreator facts: {growth_context}" if growth_context else "")


def _groq_completion(question: str, post=None, growth_context=None) -> str | None:
    """Ask Groq for a concise answer, returning None on provider failure."""
    if not GROQ_API_KEY:
        return None
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.4,
        "max_completion_tokens": 500,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Luma Assistant inside an Instagram-style app. "
                    "Answer questions clearly and briefly. You can answer general "
                    "questions, caption improvements, hashtags, content ideas, and "
                    "weekly plans. Use the supplied creator facts for data-driven "
                    "advice. Never invent metrics or image details. Say when data "
                    "is unavailable. Do not claim to see image details unless text "
                    "describes them."
                ),
            },
            {
                "role": "user",
                "content": f"{_post_context(post, growth_context)}\n\nCreator analytics JSON: {growth_context or 'unavailable'}\n\nUser question: {question}",
            },
        ],
    }
    try:
        response = httpx.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=GROQ_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return content.strip() if isinstance(content, str) and content.strip() else None
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300].replace("\n", " ")
        logger.warning("Groq assistant request failed: status=%s detail=%s", exc.response.status_code, detail)
        return None
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Groq assistant request failed: %s", type(exc).__name__)
        return None


def _local_answer(question: str, post=None, growth_context=None) -> str:
    """Return a useful answer when Groq is unavailable."""
    text = question.strip()
    lowered = text.lower()
    if not text:
        return "Ask me about growth, captions, hashtags, content ideas, or a weekly plan."
    if post is not None and any(word in lowered for word in ("caption", "about", "post", "image", "photo")):
        caption = post.caption or "No caption was added."
        author = getattr(post.author, "username", "another user")
        return f"This post is by @{author}. Caption: {caption}. I cannot identify every detail inside the image yet."
    if growth_context and any(word in lowered for word in ("analytics", "performance", "engagement", "followers", "growth")):
        if growth_context["posts"]:
            engagement = growth_context["total_likes"] + growth_context["total_comments"]
            return (f"From your {growth_context['posts']} published post(s), I can verify {growth_context['followers']} follower(s), "
                    f"{growth_context['total_likes']} like(s), and {growth_context['total_comments']} comment(s) "
                    f"({engagement} total interactions). Your average is {growth_context['avg_likes']} likes and {growth_context['avg_comments']} comments per post.")
        return "I don’t have enough published-post activity yet to calculate performance. Publish a post and I can analyze its real likes and comments."
    if growth_context and any(word in lowered for word in ("hashtag", "caption", "content idea", "weekly", "plan")):
        tags = ", ".join(f"#{tag}" for tag, _count in growth_context["top_hashtags"])
        tag_line = f"Your most-used tags are {tags}." if tags else "You have no hashtags in published captions yet."
        return (f"{tag_line} For a practical weekly test: Monday educational tip, Wednesday behind-the-scenes, "
                "Friday community question, and Sunday recap. Keep the topic consistent, add one clear call to action, "
                "and compare each post's real likes/comments after publishing. I can tailor the plan further when more history is available.")
    if "like" in lowered or "unlike" in lowered:
        return "Click the heart button on a post. Clicking it again removes your like."
    if "comment" in lowered:
        return "Type your message in the comment box under a post and press Post."
    if "feed" in lowered or "refresh" in lowered:
        return "Your feed combines recent posts with follow, engagement, and interest signals."
    if "chat" in lowered or "message" in lowered:
        return "Open Chat from the navigation, choose a user, and send a private message."
    if re.search(r"\b(hello|hi|hey)\b", lowered):
        return "Hey! I’m Luma Assistant. Ask me about a post or how Luma works."
    return "I’m Luma Assistant. Ask me about a post, a topic, or how to use the app."


def answer_question(question: str, post=None, db=None, user=None) -> str:
    """Use Groq for natural answers and never leave the UI without a response."""
    text = question.strip()
    if not text:
        return _local_answer(text, post)
    growth_context = build_growth_context(db, user) if db is not None and user is not None else None
    return _groq_completion(text, post, growth_context) or _local_answer(text, post, growth_context)
