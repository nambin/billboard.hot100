# LLM-based Match Rescue — Implementation Notes

## Goal

When the heuristic matcher in [billboard_sync/matcher.py](billboard_sync/matcher.py) fails to find an acceptable YouTube Music track for a Billboard chart entry, fall back to an LLM that examines the same (and optionally a wider) set of candidates and picks the right one. This is opt-out (`--no-llm`) since the LLM rescue typically recovers 2–5 skipped entries per weekly run that are otherwise lost.

The LLM never overrides a successful heuristic match — it only fires on heuristic skip.

## Two-phase rescue

When `_resolve_entry` reaches the "no acceptable match" state with a non-empty candidates list, it enters the rescue.

### Phase 1 — Re-rank existing candidates

Send the Billboard entry plus the same candidates the heuristic already rejected. **No new YouTube Music search call.** The LLM is told:

- One or more candidates may be the correct match even though the heuristic gates failed (typically because of artist-string formatting like `"HUNTR/X: EJAE, ..."` or separators like `"With"` that the heuristic doesn't split on).
- Return the `video_id` of the chosen candidate, or null.

Fixes (expected): tracks like **"Stateside"** (`PinkPantheress With Zara Larsson`) and **"Golden"** (`HUNTR/X: EJAE, ...`) — where the right candidate was already in the top-5 song-filtered results but the heuristic's artist-substring check rejected it.

### Phase 2 — Widened search

Only runs if Phase 1 returned null. Do **one** fresh `ytmusicapi.search(query, filter=None)` call (no `filter="songs"`), so the candidates now include `video` and `album` kinds in addition to `song`. Send those candidates to the LLM with the same prompt; same JSON output schema.

Fixes (expected): tracks like **"Sleepless In A Hotel Room"** (Luke Combs), where YT Music indexes the official audio only as a `video` early after release.

### Empty heuristic results

If the original `filter="songs"` search returned zero candidates (rare but possible), skip Phase 1 entirely and go directly to Phase 2. This avoids pointless empty LLM calls.

### Cap

At most **two** LLM calls per skipped chart entry (Phase 1 and Phase 2). No further re-tries, no query rewriting in this iteration.

## Provider: Google Gemini

- **SDK:** `google-genai` (the unified Python SDK; replaces the older `google-generativeai`).
- **Model:** `gemini-2.5-flash-lite` by default (cheap, fast, JSON-structured-output capable). Overridable via `--llm-model`.
- **Auth:** `GEMINI_API_KEY` environment variable. The SDK's `genai.Client()` constructor reads it automatically.
- **Endpoint:** Google AI Studio (not Vertex AI) — the personal-use free-tier path.
- **JSON output:** request via `config={"response_mime_type": "application/json", "response_schema": ...}`.

## LLM call shape

A single function `LLMMatcher.pick_match(entry, candidates) -> Optional[SearchResult]`.

### Prompt template

```
You are matching a Billboard Hot 100 chart entry to one of several YouTube Music
search results. Your job: pick the candidate whose audio is the same recording as
the Billboard entry, or return null if none match.

Billboard entry:
  Rank:   {rank}
  Title:  {title}
  Artist: {artist}

YouTube Music candidates:
  {index}. video_id={video_id}
     title="{title}"
     artists=[{artist1, artist2, ...}]
     kind={song|video|album|artist}
  ...

Rules:
- Prefer a candidate whose title equals or contains the Billboard title (ignoring
  case, punctuation, and parenthetical suffixes like "(from KPop Demon Hunters)"
  or "(feat. Artist)").
- Accept a candidate whose listed artists include the Billboard primary artist
  even when the Billboard artist string contains group prefixes ("HUNTR/X:
  EJAE, ...") or separators ("With", "x", "vs.") that aren't strictly matched
  by the heuristic.
- Prefer kind="song" over kind="video" when both reference the same track,
  but accept kind="video" if it is the only listing for an official audio.
- Reject candidates that are live versions, remixes, or covers when a studio
  recording is also present.
- If no candidate is the same recording, return null.

Respond ONLY in JSON, no prose. Schema:
  {"video_id": "<picked video_id>", "confidence": "high|medium|low"}
or
  {"video_id": null, "reason": "<one short sentence>"}
```

### JSON response schema

```python
{
    "type": "object",
    "properties": {
        "video_id": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
}
```

The matcher accepts the result only if `video_id` matches one of the candidate video_ids it sent. Hallucinated IDs are silently dropped (treated as no-match).

## Behaviour notes

- **Error handling:** any LLM error (auth, rate limit, malformed JSON, transient API failure) is logged in verbose mode but **does not change the exit code**. The entry is reported as skipped, same as if the LLM had returned null.
- **No retries on LLM failure:** the cost of a stale auth or rate limit is one missed entry per affected run; not worth the latency and complexity of retry-with-backoff.
- **Determinism:** call with `temperature=0` so the same chart-week + same candidates produce the same matches.
- **Idempotency:** the rescue is stateless. Re-running the binary against an unchanged Billboard chart and unchanged YT Music search results produces the same rescue picks.

## CLI surface

| Flag | Default | Meaning |
| ---- | ------- | ------- |
| `--llm` / `--no-llm` | on | Enable/disable the two-phase LLM rescue. Default on. |
| `--llm-model` | `gemini-2.5-flash-lite` | Override the Gemini model. |

### Env vars

| Var | Required when | Meaning |
| --- | -------------- | ------- |
| `GEMINI_API_KEY` | `--llm` is enabled (the default) | Google AI Studio API key. |

### Behaviour

- If `--llm` is enabled and `GEMINI_API_KEY` is unset, the binary **fails fast** at startup with exit code 1 and a clear message: pass `--no-llm` or set the env var. The whole run is aborted before any YT Music calls so the user can fix the config and re-run.

## Verbose output

The verbose log already shows each candidate the matcher examined with its reason. The LLM rescue extends this:

```
 13  Stateside                    PinkPantheress With Zara Larss ✓ matched (LLM phase 1)
       - "Stateside" by PinkPantheress, Zara Larsson [song] (score 1.00) — artist-mismatch (primary='pinkpantheress with zara larsson')
       - ... (other heuristic candidates)
       - "Stateside" by PinkPantheress, Zara Larsson [song] (score 0.00) — llm-phase1-matched (confidence=high)

 16  Sleepless In A Hotel Room    Luke Combs                     ✓ matched (LLM phase 2)
       - ... (heuristic candidates, all kind=song, all unrelated)
       - "Sleepless In A Hotel Room" by Luke Combs [video] (score 0.00) — llm-phase2-candidate
       - "Sleepless In A Hotel Room" by Luke Combs [video] (score 0.00) — llm-phase2-matched (confidence=high)
```

Non-verbose mode only shows the row-level `matched (LLM phase 1)` / `matched (LLM phase 2)` status tag, so the user can see at a glance how many rescues happened without scrolling.

## Cost estimate

- **Input tokens per call:** ~500 (prompt template + ~5 small candidate dicts).
- **Output tokens per call:** ~30.
- **Calls per run:** 0–2 per skipped entry. A typical run skips 3–5, so 3–10 calls per run.
- **Cadence:** weekly.
- **Total:** comfortably inside Gemini API's free tier; pennies/month at paid-tier rates.

## Tests

- `tests/test_llm_matcher.py` — unit tests with a fake client returning canned JSON. Covers:
  - `pick_match` returns the right `SearchResult` when LLM picks a valid video_id.
  - Returns `None` when LLM returns `{"video_id": null}`.
  - Returns `None` when LLM returns a hallucinated video_id not in the candidate list.
  - Returns `None` when the LLM client raises an exception.
- No real Gemini API calls in tests.
- Existing matcher/parser tests unchanged.

## Non-goals (this iteration)

- No query rewriting (was option B in the research; deferred).
- No multi-round LLM dialogue.
- No caching of LLM responses across runs.
- No prompt-cache use (small prompts, weekly cadence — not worth the API surface).
- No alternate provider support behind a flag (Gemini only; Claude/etc. is a future swap).
