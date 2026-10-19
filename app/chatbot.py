"""Groq-backed assistant with a deterministic local fallback."""

from __future__ import annotations

import logging
import re

import httpx

from app.config import GROQ_API_KEY, GROQ_API_URL, GROQ_MODEL, GROQ_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def _post_context(post) -> str:
    if post is None:
        return "No specific post was selected."
    author = getattr(post.author, "username", "another user")
    return f"Selected post author: @{author}\nSelected post caption: {post.caption or 'No caption was added.'}"


def _groq_completion(question: str, post=None) -> str | None:
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
                    "questions and explain selected post context. Do not claim to "
                    "see image details unless they are provided as text."
                ),
            },
            {
                "role": "user",
                "content": f"{_post_context(post)}\n\nUser question: {question}",
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


def _local_answer(question: str, post=None) -> str:
    """Return a useful answer when Groq is unavailable."""
    text = question.strip()
    lowered = text.lower()
    if not text:
        return "Ask me something about this post, the feed, likes, comments, or Luma."
    if post is not None and any(word in lowered for word in ("caption", "about", "post", "image", "photo")):
        caption = post.caption or "No caption was added."
        author = getattr(post.author, "username", "another user")
        return f"This post is by @{author}. Caption: {caption}. I cannot identify every detail inside the image yet."
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


def answer_question(question: str, post=None) -> str:
    """Use Groq for natural answers and never leave the UI without a response."""
    text = question.strip()
    if not text:
        return _local_answer(text, post)
    return _groq_completion(text, post) or _local_answer(text, post)
