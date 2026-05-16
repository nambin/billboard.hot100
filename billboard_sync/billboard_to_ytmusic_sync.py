from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
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
from billboard_sync.llm_matcher import (
    LLMError,
    LLMMatcher,
    build_default_matcher,
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


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Populate `os.environ` from `KEY=VALUE` lines in `.env`, if present.

    Existing env vars always win — so a one-off `$env:VAR =` or a value set in
    the parent shell overrides what's in the file. Lines starting with `#` and
    blank lines are skipped. Surrounding single/double quotes on the value are
    stripped. Malformed lines are silently ignored — this is a convenience
    loader, not a config parser.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
    p.add_argument(
        "--llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Two-phase Gemini rescue for entries the heuristic matcher skips. "
             "Phase 1 re-ranks the same candidates; phase 2 widens the YT search "
             "to include videos. Default: enabled. Requires GEMINI_API_KEY in env. "
             "Pass --no-llm to disable.",
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


def _build_description(chart_date_iso: str, top_n: int) -> str:
    return (
        f"Top {top_n}, week of {_format_human_date(chart_date_iso)}.\n"
        f"https://www.billboard.com/charts/hot-100/"
    )


def _build_title(chart_date_iso: str) -> str:
    """e.g. 'Billboard Hot 100 (May 16th, 2026)'. Uses the chart-week date, not
    the sync date — the title identifies which chart the playlist represents."""
    return f"Billboard Hot 100 ({_format_human_date(chart_date_iso)})"


def _format_candidate(r: SearchResult, score: float, reason: str) -> str:
    artists = ", ".join(a for a in r.artists if a) if r.artists else "(no artists)"
    return f'       - "{r.title}" by {artists} [{r.kind or "?"}] (score {score:.2f}) — {reason}'


@dataclass
class ResolveStats:
    """Per-entry counters surfaced to the caller for end-of-run summary.

    `outcome` bins the entry into one of four buckets:
      - "heuristic"  : heuristic matcher accepted a candidate
      - "llm-phase1" : LLM picked from the heuristic's candidates
      - "llm-phase2" : LLM picked from the widened (videos-allowed) search
      - "skipped"    : nothing acceptable found (or search/LLM errored)
    """
    yt_calls: int = 0
    llm_calls: int = 0
    outcome: str = "skipped"


def _resolve_entry(
    entry: ChartEntry,
    yt: YTMusicClient,
    search_limit: int = 5,
    llm: Optional[LLMMatcher] = None,
) -> tuple[Optional[str], str, list[tuple[SearchResult, float, str]], ResolveStats]:
    """Search YT Music for the entry and return (video_id, status, attempts, stats).

    `attempts` lists every candidate examined as (result, score_0_to_1, reason).
    Reason values include heuristic verdicts ("matched", "kind=...",
    "title-low (...)", "artist-mismatch (...)") and LLM-rescue tags
    ("llm-phase1-matched", "llm-phase2-candidate", "llm-phase2-matched").

    Flow:
    1. `search_songs` + heuristic validate. Short-circuits on first match.
    2. If `llm` is set and heuristic skipped: LLM phase 1 re-ranks the same
       candidates (no extra YT search).
    3. If LLM phase 1 declines: one widened `search_any` call, LLM phase 2 picks
       from the new (possibly video-kind) candidates.
    """
    attempts: list[tuple[SearchResult, float, str]] = []
    stats = ResolveStats()

    stats.yt_calls += 1
    try:
        results = yt.search_songs(f"{entry.title} {entry.artist}", limit=search_limit)
    except YTMusicAPIError as exc:
        return None, f"skipped (search error: {exc})", attempts, stats

    for r in results:
        ok, score, reason = validate_match(entry.title, entry.artist, r)
        attempts.append((r, score, reason))
        if ok:
            stats.outcome = "heuristic"
            return r.video_id, f"matched (score {score:.2f})", attempts, stats

    if llm is None:
        if not results:
            return None, "skipped (no search results)", attempts, stats
        return None, "skipped (no acceptable match)", attempts, stats

    # Phase 1: re-rank the same candidates with the LLM (no YT call).
    if results:
        stats.llm_calls += 1
        try:
            decision = llm.pick_match(entry, results)
            if decision.picked:
                attempts.append((
                    decision.picked,
                    0.0,
                    f"llm-phase1-matched (confidence={decision.confidence or '?'})",
                ))
                stats.outcome = "llm-phase1"
                return decision.picked.video_id, "matched (LLM phase 1)", attempts, stats
        except LLMError as exc:
            attempts.append((
                SearchResult(video_id="", title="(LLM phase 1)", artists=[], kind="—"),
                0.0,
                f"llm-phase1-error: {exc}",
            ))

    # Phase 2: widened YT search (no kind filter) + LLM.
    stats.yt_calls += 1
    try:
        wide_results = yt.search_any(
            f"{entry.title} {entry.artist}", limit=search_limit
        )
    except YTMusicAPIError as exc:
        return None, f"skipped (LLM phase 2 search error: {exc})", attempts, stats

    existing_ids = {r.video_id for r, _, _ in attempts if r.video_id}
    new_results = [r for r in wide_results if r.video_id not in existing_ids]
    for r in new_results:
        attempts.append((r, 0.0, "llm-phase2-candidate"))

    if not new_results:
        return None, "skipped (LLM phase 2 found nothing new)", attempts, stats

    stats.llm_calls += 1
    try:
        decision = llm.pick_match(entry, new_results)
        if decision.picked:
            attempts.append((
                decision.picked,
                0.0,
                f"llm-phase2-matched (confidence={decision.confidence or '?'})",
            ))
            stats.outcome = "llm-phase2"
            return decision.picked.video_id, "matched (LLM phase 2)", attempts, stats
    except LLMError as exc:
        attempts.append((
            SearchResult(video_id="", title="(LLM phase 2)", artists=[], kind="—"),
            0.0,
            f"llm-phase2-error: {exc}",
        ))

    return None, "skipped (LLM no acceptable match)", attempts, stats


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()
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

    # Build the LLM rescue matcher up-front so a missing GEMINI_API_KEY fails fast
    # *before* any expensive Billboard/YT calls. Pass --no-llm to skip the rescue.
    llm: Optional[LLMMatcher] = None
    if args.llm:
        try:
            llm = build_default_matcher()
        except LLMError as exc:
            print(f"LLM rescue init failed: {exc}", file=sys.stderr)
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
    total_yt_calls = 0
    total_llm_calls = 0
    outcome_counts = {"heuristic": 0, "llm-phase1": 0, "llm-phase2": 0, "skipped": 0}

    for entry in billboard_entries:
        try:
            video_id, status, attempts, stats = _resolve_entry(
                entry, yt, args.search_limit, llm=llm
            )
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
        total_yt_calls += stats.yt_calls
        total_llm_calls += stats.llm_calls
        outcome_counts[stats.outcome] += 1

    print("\nRun stats:")
    print(f"  YT Music search calls: {total_yt_calls}")
    print(f"  LLM calls:             {total_llm_calls}")
    print(f"  Heuristic matches:     {outcome_counts['heuristic']}")
    print(f"  LLM phase 1 matches:   {outcome_counts['llm-phase1']}")
    print(f"  LLM phase 2 matches:   {outcome_counts['llm-phase2']}")
    print(f"  Skipped:               {outcome_counts['skipped']}")

    title = _build_title(chart_date)
    description = _build_description(chart_date, args.top)

    if args.dry_run:
        print(
            f"\nPlaylist update: DRY RUN — would refresh with "
            f"{len(desired_ids)} songs ({skipped_count} skipped). No changes made."
        )
        print(f'Title:       DRY RUN — would set "{title}"')
        print(f'Description: DRY RUN — would set "{description}"')
        return EXIT_OK

    try:
        yt.clear_playlist(args.playlist_id)
        yt.add_playlist_items(args.playlist_id, desired_ids)
    except YTMusicAuthError as exc:
        print(f"\nYouTube Music auth f/Charailed during playlist update: {exc}", file=sys.stderr)
        return EXIT_AUTH_FAILURE
    except YTMusicAPIError as exc:
        print(f"\nPlaylist update failed: {exc}", file=sys.stderr)
        return EXIT_API_FAILURE

    print(f"\nPlaylist refreshed: {len(desired_ids)} songs ({skipped_count} skipped).")

    # Best-effort metadata stamp — playlist tracks are already correct, so a
    # failure here is logged and does not change the exit code.
    try:
        yt.set_metadata(args.playlist_id, title=title, description=description)
        print(f"Title updated:       {title}")
        print(f"Description updated: {description}")
    except YTMusicAuthError as exc:
        print(f"Metadata update skipped — auth failed: {exc}", file=sys.stderr)
    except YTMusicAPIError as exc:
        print(f"Metadata update skipped — API error: {exc}", file=sys.stderr)

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
