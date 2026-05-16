"""LLM-based fallback for the heuristic matcher.

See ../prompt-llm-retry.md for design notes.

This module is intentionally pure-Python with the Gemini client injected. The
prompt builder and response parser are stand-alone functions so tests can drive
them without touching the network — see tests/test_llm_matcher.py.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from billboard_sync.billboard import ChartEntry
from billboard_sync.matcher import SearchResult

DEFAULT_MODEL = "gemini-flash-lite-latest"


class LLMError(Exception):
    """Raised for any LLM failure — auth, network, malformed response, etc."""


@dataclass
class LLMDecision:
    """Result of one LLM call.

    `picked` is None if the LLM declined to match.
    `confidence` and `reason` are best-effort strings for verbose logging.
    """

    picked: Optional[SearchResult]
    confidence: str = ""
    reason: str = ""


class _GenAIClient(Protocol):
    """Minimal subset of `google.genai.Client` we depend on.

    Defining it as a Protocol lets tests pass a fake without importing the SDK.
    """

    def generate_response_json(self, *, model: str, prompt: str) -> str:
        ...


def _build_prompt(entry: ChartEntry, candidates: list[SearchResult]) -> str:
    """Assemble the prompt text. Pure — easy to snapshot-test."""
    lines = [
        "You are matching a Billboard Hot 100 chart entry to one of several "
        "YouTube Music search results. Your job: pick the candidate whose "
        "audio is the same recording as the Billboard entry, or return null "
        "if none match.",
        "",
        "Billboard entry:",
        f"  Rank:   {entry.rank}",
        f"  Title:  {entry.title}",
        f"  Artist: {entry.artist}",
        "",
        "YouTube Music candidates:",
    ]
    for i, c in enumerate(candidates, start=1):
        artists = ", ".join(a for a in c.artists if a) or "(no artists)"
        lines.append(
            f"  {i}. video_id={c.video_id}\n"
            f"     title=\"{c.title}\"\n"
            f"     artists=[{artists}]\n"
            f"     kind={c.kind or 'unknown'}"
        )
    lines.extend([
        "",
        "Rules:",
        "- Prefer a candidate whose title equals or contains the Billboard "
        "title (ignoring case, punctuation, and parenthetical suffixes like "
        '"(from KPop Demon Hunters)" or "(feat. Artist)").',
        "- Accept a candidate whose listed artists include the Billboard "
        "primary artist even when the Billboard artist string contains group "
        'prefixes ("HUNTR/X: EJAE, ...") or separators ("With", "x", "vs.") '
        "that aren't strictly matched by the heuristic.",
        "- Prefer kind=\"song\" over kind=\"video\" when both reference the "
        "same track, but accept kind=\"video\" if it is the only listing for "
        "an official audio.",
        "- Reject candidates that are live versions, remixes, or covers when "
        "a studio recording is also present.",
        "- If no candidate is the same recording, return null.",
        "",
        "Respond ONLY in JSON, no prose. Schema:",
        '  {"video_id": "<picked video_id>", "confidence": "high|medium|low"}',
        "or",
        '  {"video_id": null, "reason": "<one short sentence>"}',
    ])
    return "\n".join(lines)


def _parse_response(raw: str, candidates: list[SearchResult]) -> LLMDecision:
    """Pull (video_id, confidence, reason) from the LLM's JSON.

    A hallucinated video_id (one not in the candidate list) is treated as a
    decline — same as `{"video_id": null}`. Malformed JSON raises LLMError.
    """
    try:
        data: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMError(f"LLM returned non-JSON response: {raw[:200]!r}") from exc

    video_id = data.get("video_id")
    confidence = str(data.get("confidence") or "")
    reason = str(data.get("reason") or "")

    if not video_id:
        return LLMDecision(picked=None, reason=reason or "LLM returned null")

    by_id = {c.video_id: c for c in candidates}
    picked = by_id.get(video_id)
    if not picked:
        return LLMDecision(
            picked=None,
            reason=f"LLM picked unknown video_id {video_id!r} (hallucinated)",
        )

    return LLMDecision(picked=picked, confidence=confidence, reason=reason)


class GeminiClient:
    """Thin wrapper around `google.genai.Client` exposing one method.

    Lazy-imports `google.genai` so tests that don't exercise the network path
    don't need the SDK installed.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        try:
            from google import genai  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMError(
                "google-genai is not installed. Run `pip install google-genai` "
                "or pass --no-llm to disable the LLM rescue."
            ) from exc

        self._genai = genai
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def generate_response_json(self, *, model: str, prompt: str) -> str:
        from google.genai import errors, types  # type: ignore[import-not-found]
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
        except errors.APIError as exc:
            raise LLMError(f"Gemini API error: {getattr(exc, 'message', exc)}") from exc
        text = response.text
        if not text:
            raise LLMError("Gemini returned empty response body")
        return text


class LLMMatcher:
    """Pick a match from a candidate list via an LLM.

    Provider-agnostic: `client` only needs to implement `generate_response_json`. Tests
    pass a fake; production passes a `GeminiClient`.
    """

    def __init__(self, client: _GenAIClient, model: str = DEFAULT_MODEL) -> None:
        self._client = client
        self._model = model

    def pick_match(
        self, entry: ChartEntry, candidates: list[SearchResult]
    ) -> LLMDecision:
        """Run one LLM call. Returns LLMDecision; never raises on a decline.

        Raises LLMError if the underlying client fails (auth, network, etc.)
        or if the response can't be parsed as JSON.
        """
        if not candidates:
            return LLMDecision(picked=None, reason="no candidates supplied")
        prompt = _build_prompt(entry, candidates)
        raw = self._client.generate_response_json(model=self._model, prompt=prompt)
        return _parse_response(raw, candidates)


def build_default_matcher(
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
) -> LLMMatcher:
    """Factory: real Gemini-backed matcher using GEMINI_API_KEY from env if
    no key is passed. Raises LLMError if the key is missing.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError(
            "GEMINI_API_KEY is not set. Export it, pass --no-llm to disable "
            "the rescue, or pass --auth-file alongside a different key source."
        )
    return LLMMatcher(client=GeminiClient(api_key=key), model=model)
