from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

TITLE_RATIO_THRESHOLD = 70

@dataclass
class SearchResult:
    video_id: str
    title: str
    artists: list[str] = field(default_factory=list)
    kind: str = "song"


def primary_artist(artist: str) -> str:
    # Strip the "secondary artists" tail so the caller can substring-check the
    # single leading name against YouTube Music's per-artist list (YT Music
    # lists collaborators as separate entries, not as one combined string).
    # Two alternations:
    #   1. `\s+(featuring|feat|feat.|&)\s+...`  → " & Morgan Wallen", " feat. SZA"
    #   2. `\s*,\s*...`                         → ", SZA"
    # Example: "Kendrick Lamar & SZA" → "Kendrick Lamar".
    # Known gap: separators like " With ", " x ", " ft. " aren't covered yet.
    _PRIMARY_SPLIT = re.compile(
        r"\s+(?:featuring|feat\.?|&)\s+.*|\s*,\s*.*",
        re.IGNORECASE,
    )
    return _PRIMARY_SPLIT.sub("", artist).strip()


def _title_score(billboard_title: str, ytm_title: str) -> float:
    def _normalize_text(s: str) -> str:
        s = s.lower()
        s = "".join(c for c in s if not unicodedata.category(c).startswith("P"))
        s = re.sub(r"\s+", " ", s).strip()
        return s

    a = _normalize_text(billboard_title)
    b = _normalize_text(ytm_title)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 100.0
    return fuzz.ratio(a, b)


def validate_match(
    billboard_title: str,
    billboard_artist: str,
    result: SearchResult,
) -> tuple[bool, float]:
    if result.kind != "song":
        return False, 0.0

    score = _title_score(billboard_title, result.title)
    if score < TITLE_RATIO_THRESHOLD:
        return False, score / 100.0

    primary = primary_artist(billboard_artist).lower()
    if not primary:
        return False, score / 100.0

    if not any(primary in (name or "").lower() for name in result.artists):
        return False, score / 100.0

    return True, score / 100.0
