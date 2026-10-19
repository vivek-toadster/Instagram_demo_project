"""Caption hashtag extraction for graph content metadata."""

import re
from pathlib import Path


_HASHTAG_PATTERN = re.compile(r"(?<![\w])#([A-Za-z0-9_]{1,50})")
_IMAGE_TOPIC_TAGS = {
    "goa": ["goa"],
    "bollywood": ["bollywood"],
    "bb": ["bb"],
    "got": ["got"],
    "actor": ["actor"],
    "animal": ["animals"],
    "tiger": ["animals", "nature"],
    "fruit": ["fruits", "food"],
    "vegitable": ["vegetables", "food"],
    "vegetable": ["vegetables", "food"],
    "gym": ["gym", "fitness"],
    "sport": ["sports"],
    "cricket": ["cricket", "sports"],
    "football": ["football", "sports"],
    "nature": ["nature"],
    "uttarakhand": ["travel", "nature"],
    "office": ["office", "work"],
    "perfume": ["fashion", "lifestyle"],
    "stock": ["stocks", "finance"],
    "family": ["family"],
    "food": ["food"],
}


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


def hashtags_for_image(filename: str, limit: int = 5) -> list[str]:
    """Infer bounded topic tags from an uploaded/imported image filename."""
    stem = re.sub(r"[^a-z0-9]+", " ", Path(filename).stem.lower())
    tags: list[str] = []
    for word in stem.split():
        normalized_word = re.sub(r"\d+$", "", word)
        topic_tags = _IMAGE_TOPIC_TAGS.get(word, []) or _IMAGE_TOPIC_TAGS.get(normalized_word, [])
        if normalized_word == "fruits":
            topic_tags = ["fruits", "food"]
        for tag in topic_tags:
            if tag not in tags:
                tags.append(tag)
            if len(tags) >= limit:
                return tags
    if not tags:
        tags.append("photo")
    return tags[:limit]
