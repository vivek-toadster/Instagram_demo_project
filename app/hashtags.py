"""Caption hashtag extraction for graph content metadata."""

import re


_HASHTAG_PATTERN = re.compile(r"(?<![\w])#([A-Za-z0-9_]{1,50})")


def extract_hashtags(caption: str | None, limit: int = 30) -> list[str]:
    """Return unique, normalized hashtag names in caption order."""
    if not caption:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for match in _HASHTAG_PATTERN.finditer(caption):
        value = match.group(1).lower()
        if value not in seen:
            seen.add(value)
            result.append(value)
        if len(result) >= limit:
            break
    return result

