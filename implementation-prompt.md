# Billboard Hot 100 → YouTube Music Sync — Implementation Prompt

## Goal

Build a Python CLI that, given a YouTube Music playlist ID and auth credentials, fetches the current week's Billboard Hot 100 top 30 and replaces the playlist's contents with the YouTube Music equivalents in chart order. Songs not findable on YouTube Music are skipped silently. A persistent JSON cache reuses prior weeks' search results to avoid redundant lookups.

This is the v1 deliverable: a single-shot, manually-invoked binary. A scheduled weekly job (cron / Task Scheduler / GitHub Actions) will be layered on top of this CLI later — so the binary must be idempotent and safe to re-run.

## Inputs

CLI flags only — no config file, no env vars:

| Flag             | Required | Default               | Meaning                                                                                                                                |
| ---------------- | -------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `--playlist-id`  | yes      | —                     | YouTube Music playlist ID (the opaque string after `list=` in the playlist URL).                                                       |
| `--auth-file`    | yes      | —                     | Path to a `browser.json` produced by `ytmusicapi browser` (one-time interactive setup; documented in README).                         |
| `--top`          | no       | `30`                  | Number of chart positions to sync. Allowed range: 1–100.                                                                               |
| `--cache-file`   | no       | `./cache/songs.json`  | JSON file used to memoize Billboard→YouTube-Music resolutions across runs.                                                             |
| `--dry-run`      | no       | off                   | Fetch chart, resolve matches, print the report — but make no playlist edits and no cache writes.                                        |

## High-level flow

1. **Fetch chart** — GET `https://www.billboard.com/charts/hot-100/`. Parse the top N entries into `(rank, title, artist)` tuples and extract the chart issue date from the page so the report can label the week.

   Billboard rewrites their page HTML periodically and old scrapers break. **Isolate the parser** as a single function — `parse_billboard_hot_100(html: str, top_n: int) -> list[ChartEntry]` — so when it breaks, only one file changes. The rest of the codebase must not depend on Billboard's DOM shape.

2. **Load cache** — read `--cache-file` if it exists; otherwise start with an empty cache. Schema below.

3. **Resolve each chart entry to a YouTube Music video ID**:

   - **Positive cache hit**: if `(normalized_title, normalized_artist)` is in the cache with a `video_id` and `last_seen_week ≥ today − 90 days`, use that ID. No search call.
   - **Cache miss**: call `YTMusic.search(f"{title} {artist}", filter="songs")`. Validate the top result against the criteria below; if invalid, scan the next 4. If any pass, cache the `video_id` and update `last_seen_week`. If none pass, skip the song without writing to the cache — the binary runs weekly, so the same title gets retried on the next chart pull.

4. **Build the new playlist contents** — list of YouTube Music video IDs in Billboard rank order, omitting unmatched entries.

5. **Replace playlist contents** — clear the playlist's existing items, then add the new list in order. Use `YTMusic.get_playlist` to read current contents, `YTMusic.remove_playlist_items` to clear, `YTMusic.add_playlist_items` to add. The playlist's `title`, `description`, `privacy`, and thumbnail are never modified.

6. **Persist cache + print report** — flush updated cache to `--cache-file` (atomic write: write to `*.tmp`, then rename), print a human-readable summary to stdout.

7. **Idempotency** — running the binary twice in a row against an unchanged Billboard chart must produce a near no-op on the second run: zero search calls (full cache hits), zero playlist edits if the chart hasn't moved.

## Data model

### `ChartEntry`

```python
@dataclass(frozen=True)
class ChartEntry:
    rank: int       # 1..top_n
    title: str      # raw title as printed on Billboard
    artist: str     # raw artist string, often "Primary Featuring Secondary"
```

### Cache file (JSON)

```json
{
  "version": 1,
  "updated_at": "2026-05-09T14:03:21Z",
  "entries": {
    "<normalized_key>": {
      "billboard_title": "FLOWER",
      "billboard_artist": "JISOO",
      "video_id": "abc123XYZ",
      "ytm_title": "FLOWER",
      "ytm_artist": "JISOO",
      "match_score": 0.95,
      "last_seen_week": "2026-05-09",
      "first_resolved": "2026-04-12"
    }
  }
}
```

`<normalized_key>` is a deterministic string built from `(title, artist)` via:

1. Lowercase.
2. Strip ASCII punctuation and Unicode punctuation categories.
3. Collapse runs of whitespace to a single space; trim.
4. Drop trailing `feat.…`, `featuring …`, `with …`, and ` & <name>` segments from the artist (keep only the primary artist).
5. Concatenate as `"<title>||<artist>"`.

The same normalization runs on every Billboard input and every cache lookup. Treat it as a small utility (`cache.normalize_key(title, artist) -> str`) and unit-test it.

### Match validation

A YouTube Music search result is an acceptable match for a Billboard `(title, artist)` iff **all** of:

- Result kind is `song` (not `video`, not `album`, not `artist`).
- `rapidfuzz.fuzz.ratio(normalized(result.title), normalized(billboard.title)) ≥ 70`, OR one normalized title is a substring of the other.
- At least one of the result's listed artist names contains the primary Billboard artist (case-insensitive substring), where "primary" means everything before the first `Featuring`/`feat.`/`&`/`,`.

If the top search result fails, try the 2nd, 3rd, 4th, 5th. If none of the top 5 pass, skip the song for this run without writing a cache entry; it will be retried on the next weekly invocation.

## Authentication

`ytmusicapi` browser-headers, **not** OAuth, for v1. One-time setup, external to this binary:

```bash
pip install ytmusicapi
ytmusicapi browser
# follow prompts: paste request headers from a logged-in YouTube Music browser session
# this writes browser.json in the current directory
```

The user passes the path to that file via `--auth-file`. Document this setup in `README.md`.

Browser cookies eventually expire (months, typically). When `ytmusicapi` raises an auth-related exception, the binary must exit non-zero with a clear message instructing the user to re-run `ytmusicapi browser`. Do not silently retry auth failures.

## Output

Single human-readable table to stdout. Example:

```
Billboard Hot 100 — week of 2026-05-09
Resolving top 30…

  1  Pink Pony Club               Chappell Roan        ✓ matched (cached)
  2  Ordinary                     Alex Warren          ✓ matched (search, score 0.94)
  3  Luther                       Kendrick Lamar & SZA ✓ matched (cached)
  …
 18  Some Obscure Track           Some Artist          ✗ skipped (no acceptable match)
  …

Playlist update: removed 12, added 14, kept 16 — final length 28 (2 skipped).
Cache: 47 entries (3 added).
```

`--dry-run` prints the same table but ends with `Playlist update: DRY RUN — no changes made.` and writes no cache file.

Exit codes: `0` success (including with skipped songs), `1` user error (bad flags, auth file missing), `2` Billboard parse failure, `3` YouTube Music auth failure, `4` network/API failure after retries.

## Error handling

- **Billboard parse failure**: exit `2` with a message naming the parser module. Do NOT touch the playlist or cache.
- **YouTube Music auth failure**: exit `3` with instructions to re-run `ytmusicapi browser`.
- **Network errors on individual HTTP calls**: retry up to 3 times with exponential backoff (1s, 2s, 4s). After 3 failures on the Billboard fetch → exit `4`. After 3 failures on a single search → skip that song for this run and continue (no cache write either way).
- **Playlist edit failure mid-update**: the playlist must not be left half-replaced. Strategy: read the current playlist contents first; compute the full diff (removes + adds); if any single API call in the apply phase fails, attempt a best-effort revert to the pre-run state and exit non-zero. (`ytmusicapi` doesn't expose transactions, so this is best-effort.)
- **Cache write failure**: log the error but exit `0` if the playlist update succeeded — a stale cache is recoverable, an inconsistent playlist isn't.

## Project layout

```
billboard.hot100/
├── README.md                   # setup (ytmusicapi browser), invocation examples, troubleshooting
├── pyproject.toml              # dependencies + console_scripts entry point `billboard-sync`
├── billboard_sync/
│   ├── __init__.py
│   ├── __main__.py             # python -m billboard_sync …
│   ├── cli.py                  # argparse + orchestration
│   ├── billboard.py            # parse_billboard_hot_100() — the brittle layer
│   ├── ytmusic.py              # search + playlist editing
│   ├── cache.py                # load/save JSON cache + normalize_key()
│   └── matcher.py              # validate_match()
├── tests/
│   ├── fixtures/
│   │   └── billboard_sample.html
│   ├── test_billboard_parser.py
│   ├── test_cache_normalize.py
│   └── test_matcher.py
└── cache/
    └── .gitkeep
```

Note: the package import name (`billboard_sync`) uses an underscore because Python module names cannot contain dots; the repo directory uses the dotted form `billboard.hot100` per project convention.

"Binary" = a console-script entry point installable via `pip install -e .` and runnable as `billboard-sync --playlist-id … --auth-file …`. PyInstaller-style single-file packaging is out of scope for v1.

## Suggested dependencies

- Python ≥ 3.11
- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) — YouTube Music client.
- [`requests`](https://requests.readthedocs.io/) — Billboard fetch.
- [`beautifulsoup4`](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing.
- [`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz) — fuzzy title matching.
- Standard library only for the rest: `argparse`, `json`, `pathlib`, `dataclasses`, `datetime`, `re`, `sys`.
- `pytest` for tests.

## Acceptance tests

1. **Parser unit test** — given the saved snapshot in `tests/fixtures/billboard_sample.html`, `parse_billboard_hot_100(html, 30)` returns exactly 30 entries with stable rank/title/artist values. This is the *only* test that must exist for v1; the parser is the most fragile layer and the rest is plumbing.

2. **Normalization unit test** — `normalize_key("FLOWER", "JISOO")` equals `normalize_key("Flower!", "Jisoo  ")`. `normalize_key("Luther", "Kendrick Lamar & SZA")` equals `normalize_key("Luther", "Kendrick Lamar")`. Cover at least: punctuation stripping, case-folding, whitespace collapse, "feat./&" tail removal.

3. **Idempotent run (manual)** — run twice in a row within the same chart week. Second run reports `removed 0, added 0` and zero search calls in the log.

4. **Cache reuse (manual)** — delete `cache/songs.json`, run; observe `cache: N entries (N added)`. Re-run immediately; observe `cache: N entries (0 added)` and zero search calls.

5. **Dry run is read-only (manual)** — `--dry-run` produces no changes to the playlist (verify in the YouTube Music UI) and no changes to the cache file (compare hash before/after).

## Non-goals (v1)

- No scheduling — no cron, no daemon, no Task Scheduler, no GitHub Actions workflow. The binary runs once and exits.
- No multi-playlist support — one playlist per invocation.
- No UI — CLI only.
- No reading/writing playlist metadata other than its track list.
- No Spotify, Apple Music, or other backends.
- No reordering optimization — replace-mode is a full clear-and-rebuild every time.
- No retry-on-stale-auth — surface the error, the user re-runs `ytmusicapi browser`.
- No Billboard 200, Global 200, or other charts.

## Decisions already made

- **Replace, not merge** — songs that fall off the chart drop from the playlist; songs that re-enter come back. No archive growth.
- **Top 30 default, configurable up to 100** — `--top 100` works; `--top 200` is rejected.
- **Browser-headers auth, not OAuth** — simpler one-time setup. Revisit only if cookie expiry becomes a chronic pain point.
- **JSON cache, not SQLite** — single file, deterministic key, debuggable in any text editor. Migrate to SQLite only if entry count grows beyond a few thousand or read patterns demand indexed lookups.
- **Skip unmatched silently** — songs not findable on YouTube Music are omitted from the playlist; the report logs them but the run still exits `0`. Per user direction.
- **Parser is a single isolated function** — Billboard's HTML *will* change. Keeping the parser to one file with one fixture-backed unit test is the only sustainable defense.

## Future work (out of scope for v1)

- Weekly scheduling: GitHub Actions cron, or Windows Task Scheduler, or a tiny long-running Python daemon. Pick the cheapest one (likely GitHub Actions if the runner has internet to YouTube Music — or Task Scheduler on the user's PC for personal use).
- OAuth migration if browser headers expire too often.
- Historical archive: a second "all-time top 30 hits" playlist that uses merge-mode instead of replace.
- HTML report committed to the user's GitHub Pages site after each run (similar pattern to `nambin.github.io`).
- Other charts: Billboard 200, Global 200, country charts.
