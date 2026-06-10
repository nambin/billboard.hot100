# billboard-to-ytmusic-sync

Replace a YouTube Music playlist's contents with the current Billboard Hot 100 top N, in chart order. Songs not findable on YouTube Music are skipped silently.

https://www.billboard.com/charts/hot-100/

Top 30 songs are regularly deplyed to this public [Youtube Music Playlist](https://music.youtube.com/playlist?list=PL2qJd7QV51AbSavNCq9E4tmJY3cDYHBbY) via GitHub Action. 

## Install

```bash
pip install -e .
```

Requires Python ≥ 3.11.

## Auth file

The tool needs a `browser.json` containing your YouTube Music session cookies for `ytmusicapi` to act on your behalf. Generate it once and pass its path via `--auth-file` (or place it at `./browser.json` to use the default). Browser cookies eventually expire — when they do, every authenticated write fails with HTTP 401 and the tool exits with code 3. Regenerate the file to fix it.

### Generating `browser.json`

`ytmusicapi`'s setup tool builds the file from the request headers of a logged-in YouTube Music session:

1. Open <https://music.youtube.com> in your browser, signed in to the account you want to sync.
2. Open DevTools (F12) → **Network** tab, filter for `browse`.
3. Click any **POST** request to `youtubei/v1/browse`.
4. Copy that request's headers. Right-click → **Copy → Copy as fetch (Node.js)**, paste into a text editor, and pull out just the headers object.
5. Verify it's authenticated — you should see your account name, not a "Get Music Premium" menu:

   ```bash
   python -c "from ytmusicapi import YTMusic; print(YTMusic('browser.json').get_account_info())"
   ```

`browser.json` is gitignored. Don't commit it.

## Usage

Both `--playlist-id` and `--auth-file` have defaults baked into the source. Override either when needed:

```bash
billboard-to-ytmusic-sync                                          # uses defaults
billboard-to-ytmusic-sync --playlist-id PLxxx --auth-file ./other.json
billboard-to-ytmusic-sync --dry-run                                # safe preview
```

Flags:

| Flag             | Default                      | Notes                                                  |
| ---------------- | ---------------------------- | ------------------------------------------------------ |
| `--playlist-id`  | hard-coded in source         | Override with the opaque string after `list=` in the playlist URL. |
| `--auth-file`    | `./browser.json`             | Path to the YouTube Music auth `browser.json`.         |
| `--top`          | `30`                         | 1–100.                                                 |
| `--search-limit` | `5`                          | 1–20. Candidates examined per chart entry.             |
| `--dry-run`      | off                          | Resolve and print the report; no playlist edits.       |
| `-v` / `--verbose` | off                        | Per-entry candidate list with match reasons.           |
| `--llm` / `--no-llm` | **on**                   | Two-phase Gemini rescue for heuristic skips. Needs `GEMINI_API_KEY`. |

## LLM rescue

When the heuristic matcher can't find an acceptable YT Music track, the binary falls back to Gemini in two phases:

1. **Re-rank** the same candidates the heuristic already saw (no extra YT call).
2. If phase 1 declines, do **one** widened YT search (no `filter="songs"`) and let Gemini pick from those.

Default is on — pass `--no-llm` to skip the rescue. Requires `GEMINI_API_KEY` set in the environment; get one at <https://aistudio.google.com/app/apikey>. Free tier easily covers a weekly run. The model (`gemini-flash-lite-latest`) is hard-coded in [billboard_sync/llm_matcher.py](billboard_sync/llm_matcher.py); edit `DEFAULT_MODEL` there to change it. See [prompt-llm-retry.md](prompt-llm-retry.md) for the design.

### Setting `GEMINI_API_KEY`

The binary auto-loads `KEY=VALUE` pairs from a `.env` file in the working directory on startup. Existing env vars always win over the file, so you can override ad-hoc.

```bash
cp .env.example .env
# edit .env, paste your key after `GEMINI_API_KEY=`
billboard-to-ytmusic-sync --dry-run -v
```

`.env` is gitignored. Don't commit it.

Alternatives:
- `$env:GEMINI_API_KEY = "..."` for one shell session.
- `[Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "...", "User")` to persist for your user across all future shells.

## Example output

```
Billboard Hot 100 — week of 2026-05-16
Resolving top 30…

  1  Choosin' Texas               Ella Langley                   ✓ matched (score 1.00)
  2  Be Her                       Ella Langley                   ✓ matched (score 1.00)
  …
 13  Stateside                    PinkPantheress With Zara Larss ✓ matched (LLM phase 1)
  …
 16  Sleepless In A Hotel Room    Luke Combs                     ✓ matched (LLM phase 2)
  …
 18  Some Obscure Track           Some Artist                    ✗ skipped (LLM no acceptable match)

Run stats:
  YT Music search calls: 32
  LLM calls:             3
  Heuristic matches:     27
  LLM phase 1 matches:   1
  LLM phase 2 matches:   1
  Skipped:               1

Playlist refreshed: 29 songs (1 skipped).
Title updated:       Billboard Hot 100 (May 16th, 2026)
Description updated: Top 30, week of May 16th, 2026.
https://www.billboard.com/charts/hot-100/
```

Use `-v` / `--verbose` to print every YT Music candidate the matcher (and LLM) examined per chart entry, with the reason each was accepted or rejected. `--dry-run` prints the same report but ends with `Playlist update: DRY RUN — would refresh with N songs (M skipped). No changes made.` and makes no playlist edits.

## Exit codes

| Code | Meaning                                     |
| ---- | ------------------------------------------- |
| `0`  | Success (including runs with skipped songs). |
| `1`  | User error (bad flags, missing auth file).  |
| `2`  | Billboard parse failure.                    |
| `3`  | YouTube Music auth failure — regenerate `browser.json`.   |
| `4`  | Network/API failure after retries.          |

## Tests

```bash
pip install -e .[dev]
pytest
```

The parser unit test (`tests/test_billboard_parser.py`) is the most important one — Billboard rewrites their HTML periodically and `billboard_sync/billboard.py` is the only file that should need to change when it does.

## Troubleshooting

- **Exit code 1, "LLM rescue init failed: GEMINI_API_KEY is not set"**: put the key in `.env` (copy from `.env.example`), set it in the environment, or pass `--no-llm` to disable the rescue.
- **Exit code 2 (parse failure)**: Billboard changed their HTML. Update the selectors in [billboard_sync/billboard.py](billboard_sync/billboard.py) and add a fresh weekly snapshot under [tests/fixtures/](tests/fixtures/).
- **Exit code 3 (auth failure)**: cookies expired. Regenerate `browser.json` and point `--auth-file` at the new file.
