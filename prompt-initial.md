# Billboard Hot 100 → YouTube Music Sync — Implementation Notes

## Goal

A Python CLI (`billboard-to-ytmusic-sync`) that, given a YouTube Music playlist ID and auth credentials, fetches the current week's Billboard Hot 100 top N and replaces the playlist's contents with the YouTube Music equivalents in chart order. Songs not findable on YouTube Music are skipped silently.

This is a single-shot, manually-invoked binary. A scheduled weekly job (cron / Task Scheduler / GitHub Actions) can be layered on top later.

## Inputs

CLI flags only — no config file, no env vars. Both `--playlist-id` and `--auth-file` have defaults hard-coded in the source so a no-arg run works for the project owner.

| Flag             | Default                              | Meaning                                                                     |
| ---------------- | ------------------------------------ | --------------------------------------------------------------------------- |
| `--playlist-id`  | hard-coded constant                  | YouTube Music playlist ID (the opaque string after `list=` in the URL).     |
| `--auth-file`    | `./browser.json`                     | Path to the YouTube Music auth `browser.json` (generation method TBD).      |
| `--top`          | `30`                                 | Number of chart positions to sync. Allowed range: 1–100.                    |
| `--dry-run`      | off                                  | Fetch chart, resolve matches, print the report — but make no playlist edits. |

## High-level flow

1. **Fetch chart** — `GET https://www.billboard.com/charts/hot-100/`. Parse the top N entries into `(rank, title, artist)` tuples and extract the chart issue date so the report can label the week.

   Billboard rewrites their page HTML periodically and old scrapers break. **Isolate the parser** as a single function — `parse_billboard_hot_100(html: str, top_n: int) -> list[ChartEntry]` — so when it breaks, only one file changes. The rest of the codebase must not depend on Billboard's DOM shape.

2. **Resolve each chart entry to a YouTube Music video ID** — call `YTMusic.search(f"{title} {artist}", filter="songs")`. Validate the top result against the criteria below; if invalid, scan the next 4. If any pass, take the `video_id`. If none pass, skip the song for this run.

3. **Build the new playlist contents** — list of YouTube Music video IDs in Billboard rank order, omitting unmatched entries.

4. **Replace playlist contents** — clear the playlist's existing items, then add the new list in order. Use `YTMusic.get_playlist` to read current contents, `YTMusic.remove_playlist_items` to clear, `YTMusic.add_playlist_items` to add. The playlist's `title`, `description`, `privacy`, and thumbnail are never modified.

5. **Print report** — human-readable summary to stdout.

   Every run does a full clear-and-rebuild. There is no caching layer and no diff-vs.-current logic. Re-running the binary against an unchanged Billboard chart still issues all the search and playlist-edit API calls — accepted trade-off in exchange for simplicity.

## Data model

### `ChartEntry`

```python
@dataclass(frozen=True)
class ChartEntry:
    rank: int       # 1..top_n
    title: str      # raw title as printed on Billboard
    artist: str     # raw artist string, often "Primary Featuring Secondary"
```

### `SearchResult`

```python
@dataclass
class SearchResult:
    video_id: str
    title: str
    artists: list[str]   # all listed artists from the YouTube Music result
    kind: str            # "song", "video", "album", "artist"
```

### Match validation

A YouTube Music search result is an acceptable match for a Billboard `(title, artist)` iff **all** of:

- Result kind is `song` (not `video`, not `album`, not `artist`).
- `rapidfuzz.fuzz.ratio(normalize_text(result.title), normalize_text(billboard.title)) ≥ 70`, OR one normalized title is a substring of the other.
- At least one of the result's listed artist names contains the primary Billboard artist (case-insensitive substring), where "primary" means everything before the first `Featuring`/`feat.`/`&`/`,`.

`normalize_text` lowercases, strips punctuation, and collapses whitespace. If the top search result fails, try the 2nd, 3rd, 4th, 5th. If none pass, skip the song for this run.

## Authentication

The binary uses `ytmusicapi`'s browser-headers auth (**not** OAuth). It expects a `browser.json` containing the user's YouTube Music session headers, passed via `--auth-file`. The mechanism for generating that file is TBD — the file's existence is a prerequisite for any non-`--dry-run` invocation. Browser cookies eventually expire (months, typically). When `ytmusicapi` raises an auth-related exception, the binary exits non-zero with a clear message instructing the user to regenerate the auth file. Do not silently retry auth failures.

## Output

Single human-readable table to stdout. Example:

```
Billboard Hot 100 — week of 2026-05-09
Resolving top 30…

  1  Choosin' Texas               Ella Langley         ✓ matched (score 1.00)
  2  I Just Might                 Bruno Mars           ✓ matched (score 1.00)
  …
 18  Some Obscure Track           Some Artist          ✗ skipped (no acceptable match)
  …

Playlist refreshed: 28 songs (2 skipped).
```

`--dry-run` prints the same table but ends with `Playlist update: DRY RUN — no changes made.` and makes no API calls beyond the searches needed to print the row statuses.

Exit codes: `0` success (including with skipped songs), `1` user error (bad flags, auth file missing), `2` Billboard parse failure, `3` YouTube Music auth failure, `4` network/API failure after retries.

## Error handling

- **Billboard parse failure**: exit `2` with a message naming the parser module. Do NOT touch the playlist.
- **YouTube Music auth failure** (at any point in the run): exit `3` with instructions to regenerate the auth file.
- **Network errors on individual HTTP calls**: retry up to 3 times with exponential backoff (1s, 2s, 4s). After 3 failures on the Billboard fetch → exit `4`. After 3 failures on a single search → skip that song for this run and continue.
- **Playlist edit failure mid-update**: no revert is attempted. If `clear_playlist` succeeds and `add_playlist_items` fails, the playlist is left empty until the next successful run cleans it up. Acceptable because the binary is idempotent in the "same final state" sense — the next run will reach the desired state regardless of the previous run's failure point.

## Suggested dependencies

- Python ≥ 3.11
- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) — YouTube Music client.
- [`requests`](https://requests.readthedocs.io/) — Billboard fetch.
- [`beautifulsoup4`](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing.
- [`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz) — fuzzy title matching.
- Standard library only for the rest: `argparse`, `pathlib`, `dataclasses`, `datetime`, `re`, `unicodedata`, `sys`.
- `pytest` for tests.

## Tests

The parser is the fragile layer; the rest is plumbing. Test structure:

1. **Per-week fixture tests** — `tests/test_billboard_parser.py` defines a `ChartFixture` dataclass and a registry list `ALL_FIXTURES`. Each fixture pairs an HTML snapshot under `tests/fixtures/billboard_sample_YYYYMMDD.html` with its expected `(rank, title, artist)` rows and chart date. The parser tests are pytest-parametrized over `ALL_FIXTURES`, so adding a new chart-week is a one-line registry append plus the snapshot HTML.

2. **NEW-badge / whitespace regression** — Billboard renders debut entries with a `NEW` badge as a `.c-label` ahead of the artist label, and renders multi-anchor artist strings like `<a>X</a> &<a>Y</a>` whose text would collapse to `X &Y` under naive extraction. The parser must skip the badge and join inner text with whitespace. Covered by an inline-HTML test independent of the fixture set.

3. **Matcher tests** — `tests/test_matcher.py` covers song-kind enforcement, artist-substring matching, fuzzy title threshold, primary-artist extraction, and the substring-match shortcut.

There are no manual acceptance tests beyond running the binary against a real playlist. Idempotency is "same final state," verified by inspection rather than a no-op short-circuit.

## Non-goals

- No scheduling — no cron, no daemon, no Task Scheduler, no GitHub Actions workflow. The binary runs once and exits.
- No multi-playlist support — one playlist per invocation.
- No UI — CLI only.
- No reading/writing playlist metadata other than its track list.
- No Spotify, Apple Music, or other backends.
- No partial-update / diff optimization — replace-mode is a full clear-and-rebuild every time.
- No retry-on-stale-auth — surface the error and let the user regenerate the auth file.
- No Billboard 200, Global 200, or other charts.
- **No caching layer** — every run does a fresh search per chart entry. Simplicity beats API-call thrift at the current scale.

## Decisions already made

- **Replace, not merge** — songs that fall off the chart drop from the playlist; songs that re-enter come back. No archive growth.
- **Top 30 default, configurable up to 100** — `--top 100` works; `--top 200` is rejected.
- **Browser-headers auth, not OAuth** — simpler one-time setup. Revisit only if cookie expiry becomes a chronic pain point.
- **No cache** — earlier iteration had a JSON cache memoizing search results. Removed because the status-tracking added more complexity than the API savings justified for a weekly-cadence binary.
- **Skip unmatched silently** — songs not findable on YouTube Music are omitted from the playlist; the report logs them but the run still exits `0`.
- **Parser is a single isolated function** — Billboard's HTML *will* change. Keeping the parser to one file with fixture-backed unit tests is the only sustainable defense.
- **Always full clear-and-rebuild** — no diff with current playlist contents, no kept/removed/added counters. Guarantees chart-rank order is preserved without partial-update bookkeeping. The trade-off is N remove + N add API calls every run.
- **Defaults baked into the source** — `--playlist-id` and `--auth-file` have defaults so a no-arg invocation works. Acceptable for a single-user private repo.

## Future work (out of scope today)

- Weekly scheduling: GitHub Actions cron, or Windows Task Scheduler, or a tiny long-running Python daemon. Pick the cheapest one (likely GitHub Actions if the runner has internet to YouTube Music — or Task Scheduler on the user's PC for personal use).
- Reintroduce caching if API quota becomes a concern, or if running multiple times per week becomes common.
- OAuth migration if browser headers expire too often.
- Historical archive: a second "all-time top 30 hits" playlist that uses merge-mode instead of replace.
- HTML report committed to the user's GitHub Pages site after each run.
- Other charts: Billboard 200, Global 200, country charts.
