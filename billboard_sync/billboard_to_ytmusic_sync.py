from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from billboard_sync.billboard import (
    BillboardParseError,
    ChartEntry,
    fetch_billboard_html,
    parse_billboard_hot_100,
    parse_chart_date,
)
from billboard_sync.matcher import validate_match
from billboard_sync.ytmusic import (
    YTMusicAPIError,
    YTMusicAuthError,
    YTMusicClient,
)

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_PARSE_FAILURE = 2
EXIT_AUTH_FAILURE = 3
EXIT_API_FAILURE = 4


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="billboard-to-ytmusic-sync",
        description="Sync the Billboard Hot 100 to a YouTube Music playlist.",
    )
    p.add_argument(
        "--playlist-id",
        default="PL2qJd7QV51AbSavNCq9E4tmJY3cDYHBbY",
        help="YouTube Music playlist ID.",
    )
    p.add_argument(
        "--auth-file",
        default="./browser.json",
        help="Path to YouTube Music auth file (browser.json).",
    )
    p.add_argument("--top", type=int, default=30, help="Chart positions to sync (1-100, default 30).")
    p.add_argument("--dry-run", action="store_true", help="Resolve and report only; no playlist edits.")
    return p.parse_args(argv)


def _format_row(rank: int, title: str, artist: str, status: str) -> str:
    return f"{rank:>3}  {title[:28]:<28} {artist[:30]:<30} {status}"


def _resolve_entry(entry: ChartEntry, yt: YTMusicClient) -> tuple[Optional[str], str]:
    try:
        results = yt.search_songs(f"{entry.title} {entry.artist}", limit=5)
    except YTMusicAPIError as exc:
        return None, f"skipped (search error: {exc})"

    for r in results:
        ok, score = validate_match(entry.title, entry.artist, r)
        if ok:
            return r.video_id, f"matched (score {score:.2f})"

    return None, "skipped (no acceptable match)"


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if not 1 <= args.top <= 100:
        print(f"--top must be in 1..100 (got {args.top})", file=sys.stderr)
        return EXIT_USER_ERROR

    auth_path = Path(args.auth_file)
    if not auth_path.exists():
        print(f"--auth-file not found: {auth_path}", file=sys.stderr)
        return EXIT_USER_ERROR

    try:
        html = fetch_billboard_html()
        billboard_entries = parse_billboard_hot_100(html, args.top)
    except BillboardParseError as exc:
        print(
            f"Billboard parse failure (billboard_sync.billboard): {exc}",
            file=sys.stderr,
        )
        return EXIT_PARSE_FAILURE

    chart_date = parse_chart_date(html) or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        yt = YTMusicClient(str(auth_path))
    except YTMusicAuthError as exc:
        print(
            f"YouTube Music auth failed: {exc}\n"
            "Regenerate browser.json to refresh credentials.",
            file=sys.stderr,
        )
        return EXIT_AUTH_FAILURE

    print(f"Billboard Hot 100 — week of {chart_date}")
    print(f"Resolving top {args.top}…\n")

    # YouTube Music videoIds for matched chart entries, in Billboard rank order.
    # A videoId is the opaque 11-char string after `v=` in a YT Music URL — e.g.
    # `rW2HmFDGdKs` from https://music.youtube.com/watch?v=rW2HmFDGdKs.
    # Unmatched entries are omitted; this list is what we'll push to the playlist.
    desired_ids: list[str] = []
    skipped_count = 0

    for entry in billboard_entries:
        try:
            video_id, status = _resolve_entry(entry, yt)
        except YTMusicAuthError as exc:
            print(
                f"\nYouTube Music auth failed mid-run: {exc}\n"
                "Regenerate browser.json.",
                file=sys.stderr,
            )
            return EXIT_AUTH_FAILURE
        marker = "✓" if video_id else "✗"
        print(_format_row(entry.rank, entry.title, entry.artist, f"{marker} {status}"))
        if video_id:
            desired_ids.append(video_id)
        else:
            skipped_count += 1

    if args.dry_run:
        print(f"\nPlaylist update: DRY RUN — no changes made.")
        return EXIT_OK

    try:
        yt.clear_playlist(args.playlist_id)
        yt.add_playlist_items(args.playlist_id, desired_ids)
    except YTMusicAuthError as exc:
        print(f"\nYouTube Music auth failed during playlist update: {exc}", file=sys.stderr)
        return EXIT_AUTH_FAILURE
    except YTMusicAPIError as exc:
        print(f"\nPlaylist update failed: {exc}", file=sys.stderr)
        return EXIT_API_FAILURE

    print(f"\nPlaylist refreshed: {len(desired_ids)} songs ({skipped_count} skipped).")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
