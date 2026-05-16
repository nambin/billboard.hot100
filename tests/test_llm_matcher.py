"""Tests for the LLM rescue path. No real Gemini calls — uses a fake client."""
from __future__ import annotations

import json

import pytest

from billboard_sync.billboard import ChartEntry
from billboard_sync.llm_matcher import (
    LLMError,
    LLMMatcher,
    _build_prompt,
    _parse_response,
)
from billboard_sync.matcher import SearchResult


def _entry(rank=11, title="Golden", artist="HUNTR/X: EJAE, Audrey Nuna & REI AMI"):
    return ChartEntry(rank=rank, title=title, artist=artist)


def _candidates():
    return [
        SearchResult(video_id="abc", title="Golden", artists=["HUNTR/X", "EJAE"], kind="song"),
        SearchResult(video_id="def", title="Golden (Live)", artists=["HUNTR/X"], kind="video"),
    ]


class FakeClient:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def generate_response_json(self, *, model: str, prompt: str) -> str:
        self.calls.append((model, prompt))
        return self.payload


class FailingClient:
    def __init__(self, exc: Exception):
        self.exc = exc

    def generate_response_json(self, *, model: str, prompt: str) -> str:
        raise self.exc


def test_build_prompt_contains_entry_and_candidates():
    prompt = _build_prompt(_entry(), _candidates())
    assert "Golden" in prompt
    assert "HUNTR/X: EJAE" in prompt
    assert "video_id=abc" in prompt
    assert "video_id=def" in prompt
    assert "kind=song" in prompt
    assert "kind=video" in prompt

    # Snapshot of the whole prompt for the fixture entry+candidates. Update
    # this when you intentionally change the template — pytest's diff on
    # failure will show exactly what shifted.
    expected = (
        "You are matching a Billboard Hot 100 chart entry to one of several "
        "YouTube Music search results. Your job: pick the candidate whose "
        "audio is the same recording as the Billboard entry, or return null "
        "if none match.\n"
        "\n"
        "Billboard entry:\n"
        "  Rank:   11\n"
        "  Title:  Golden\n"
        "  Artist: HUNTR/X: EJAE, Audrey Nuna & REI AMI\n"
        "\n"
        "YouTube Music candidates:\n"
        "  1. video_id=abc\n"
        '     title="Golden"\n'
        "     artists=[HUNTR/X, EJAE]\n"
        "     kind=song\n"
        "  2. video_id=def\n"
        '     title="Golden (Live)"\n'
        "     artists=[HUNTR/X]\n"
        "     kind=video\n"
        "\n"
        "Rules:\n"
        "- Prefer a candidate whose title equals or contains the Billboard "
        "title (ignoring case, punctuation, and parenthetical suffixes like "
        '"(from KPop Demon Hunters)" or "(feat. Artist)").\n'
        "- Accept a candidate whose listed artists include the Billboard "
        "primary artist even when the Billboard artist string contains group "
        'prefixes ("HUNTR/X: EJAE, ...") or separators ("With", "x", "vs.") '
        "that aren't strictly matched by the heuristic.\n"
        '- Prefer kind="song" over kind="video" when both reference the '
        'same track, but accept kind="video" if it is the only listing for '
        "an official audio.\n"
        "- Reject candidates that are live versions, remixes, or covers when "
        "a studio recording is also present.\n"
        "- If no candidate is the same recording, return null.\n"
        "\n"
        "Respond ONLY in JSON, no prose. Schema:\n"
        '  {"video_id": "<picked video_id>", "confidence": "high|medium|low"}\n'
        "or\n"
        '  {"video_id": null, "reason": "<one short sentence>"}'
    )
    assert prompt == expected


def test_parse_response_picks_matching_video_id():
    payload = json.dumps({"video_id": "abc", "confidence": "high"})
    decision = _parse_response(payload, _candidates())
    assert decision.picked is not None
    assert decision.picked.video_id == "abc"
    assert decision.confidence == "high"


def test_parse_response_null_returns_none():
    payload = json.dumps({"video_id": None, "reason": "no match"})
    decision = _parse_response(payload, _candidates())
    assert decision.picked is None
    assert "no match" in decision.reason


def test_parse_response_hallucinated_id_treated_as_decline():
    payload = json.dumps({"video_id": "totally-made-up", "confidence": "high"})
    decision = _parse_response(payload, _candidates())
    assert decision.picked is None
    assert "hallucinated" in decision.reason.lower() or "unknown" in decision.reason.lower()


def test_parse_response_malformed_json_raises():
    with pytest.raises(LLMError):
        _parse_response("this is not json at all", _candidates())


def test_pick_match_end_to_end_with_fake_client():
    client = FakeClient(json.dumps({"video_id": "def", "confidence": "medium"}))
    matcher = LLMMatcher(client=client, model="fake-model")
    decision = matcher.pick_match(_entry(), _candidates())
    assert decision.picked is not None
    assert decision.picked.video_id == "def"
    assert client.calls and client.calls[0][0] == "fake-model"


def test_pick_match_empty_candidates_returns_none_without_calling_client():
    client = FakeClient(json.dumps({"video_id": "abc"}))
    matcher = LLMMatcher(client=client, model="fake-model")
    decision = matcher.pick_match(_entry(), [])
    assert decision.picked is None
    assert client.calls == []


def test_pick_match_propagates_llm_error():
    matcher = LLMMatcher(client=FailingClient(LLMError("auth failed")), model="m")
    with pytest.raises(LLMError):
        matcher.pick_match(_entry(), _candidates())
