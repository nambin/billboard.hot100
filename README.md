# billboard-to-ytmusic-sync

Replace a YouTube Music playlist's contents with the current Billboard Hot 100 top N, in chart order. Songs not findable on YouTube Music are skipped silently.

## Install

```bash
pip install -e .
```

Requires Python ≥ 3.11.

## Auth file

The tool needs a `browser.json` containing your YouTube Music session cookies for `ytmusicapi` to act on your behalf. Generate it once and pass its path via `--auth-file` (or place it at `./browser.json` to use the default). Browser cookies eventually expire (months, typically) — when they do, regenerate the file.

> _Generation method TBD._

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
| `--dry-run`      | off                          | Resolve and print the report; no playlist edits.       |

## Example output

```
Billboard Hot 100 — week of 2026-05-09
Resolving top 30…

  1  Pink Pony Club               Chappell Roan        ✓ matched (score 1.00)
  2  Ordinary                     Alex Warren          ✓ matched (score 0.94)
  …
 18  Some Obscure Track           Some Artist          ✗ skipped (no acceptable match)

Playlist refreshed: 28 songs (2 skipped).
```

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

- **Exit code 2 (parse failure)**: Billboard changed their HTML. Update the selectors in [billboard_sync/billboard.py](billboard_sync/billboard.py) and add a fresh weekly snapshot under [tests/fixtures/](tests/fixtures/).
- **Exit code 3 (auth failure)**: cookies expired. Regenerate `browser.json` and point `--auth-file` at the new file.
