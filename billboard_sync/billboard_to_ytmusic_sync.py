from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from billboard_sync.billboard import (
    BillboardParseError,
    ChartEntry,
    fetch_billboard_html,
    parse_billboard_hot_100,
    parse_chart_date,
)
from billboard_sync.matcher import SearchResult, validate_match
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
    p.add_argument(
        "--search-limit",
        type=int,
        default=5,
        help="How many YT Music results to examine per chart entry before giving up "
             "(1-20, default 5). Higher = more chances to find a valid match, but "
             "more noise to wade through and slower runs.",
    )
    p.add_argument("--dry-run", action="store_true", help="Resolve and report only; no playlist edits.")
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="For each chart entry, print every YT Music candidate examined with its "
             "title/artists/kind and the reason it was accepted or rejected.",
    )
    return p.parse_args(argv)


def _format_row(rank: int, title: str, artist: str, status: str) -> str:
    return f"{rank:>3}  {title[:28]:<28} {artist[:30]:<30} {status}"


def _ordinal_suffix(day: int) -> str:
    # 11th/12th/13th are exceptions to the 1st/2nd/3rd rule.
    if 10 <= day % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _format_human_date(iso: str) -> str:
    """'2026-05-09' → 'May 9th, 2026'."""
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{d.strftime('%B')} {d.day}{_ordinal_suffix(d.day)}, {d.year}"


def _build_description(chart_date_iso: str, top_n: int, sync_date_iso: str) -> str:
    return (
        f"Billboard Hot 100 top {top_n}.\n"
        f"Chart week of {_format_human_date(chart_date_iso)}.\n"
        f"Synced {_format_human_date(sync_date_iso)}."
    )


def _format_candidate(r: SearchResult, score: float, reason: str) -> str:
    artists = ", ".join(a for a in r.artists if a) if r.artists else "(no artists)"
    return f'       - "{r.title}" by {artists} [{r.kind or "?"}] (score {score:.2f}) — {reason}'


def _resolve_entry(
    entry: ChartEntry, yt: YTMusicClient, search_limit: int = 5
) -> tuple[Optional[str], str, list[tuple[SearchResult, float, str]]]:
    """Search YT Music for the entry and return (video_id, status, attempts).

    `attempts` lists every candidate examined as (result, score_0_to_1, reason)
    — useful for verbose logging. Empty if the search errored or returned no
    results. On a match, we short-circuit, so `attempts` ends at the picked
    candidate (any earlier-rejected ones are included before it).
    """
    try:
        results = yt.search_songs(f"{entry.title} {entry.artist}", limit=search_limit)
    except YTMusicAPIError as exc:
        return None, f"skipped (search error: {exc})", []

    if not results:
        return None, "skipped (no search results)", []

    attempts: list[tuple[SearchResult, float, str]] = []
    for r in results:
        ok, score, reason = validate_match(entry.title, entry.artist, r)
        attempts.append((r, score, reason))
        if ok:
            return r.video_id, f"matched (score {score:.2f})", attempts

    return None, "skipped (no acceptable match)", attempts


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if not 1 <= args.top <= 100:
        print(f"--top must be in 1..100 (got {args.top})", file=sys.stderr)
        return EXIT_USER_ERROR

    if not 1 <= args.search_limit <= 20:
        print(f"--search-limit must be in 1..20 (got {args.search_limit})", file=sys.stderr)
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
            video_id, status, attempts = _resolve_entry(entry, yt, args.search_limit)
        except YTMusicAuthError as exc:
            print(
                f"\nYouTube Music auth failed mid-run: {exc}\n"
                "Regenerate browser.json.",
                file=sys.stderr,
            )
            return EXIT_AUTH_FAILURE
        marker = "✓" if video_id else "✗"
        print(_format_row(entry.rank, entry.title, entry.artist, f"{marker} {status}"))
        if args.verbose:
            if not attempts:
                print("       (no candidates from search)")
            for r, score, reason in attempts:
                print(_format_candidate(r, score, reason))
            print()
        if video_id:
            desired_ids.append(video_id)
        else:
            skipped_count += 1

    sync_date_iso = date.today().isoformat()
    description = _build_description(chart_date, args.top, sync_date_iso)

    if args.dry_run:
        print(
            f"\nPlaylist update: DRY RUN — would refresh with "
            f"{len(desired_ids)} songs ({skipped_count} skipped). No changes made."
        )
        print(f'Description: DRY RUN — would set "{description}"')
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

    # Best-effort description stamp — playlist tracks are already correct, so a
    # failure here is logged and does not change the exit code.
    try:
        yt.set_description(args.playlist_id, description)
        print(f"Description updated: {description}")
    except YTMusicAuthError as exc:
        print(f"Description update skipped — auth failed: {exc}", file=sys.stderr)
    except YTMusicAPIError as exc:
        print(f"Description update skipped — API error: {exc}", file=sys.stderr)

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
