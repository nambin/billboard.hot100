from __future__ import annotations

import time
from typing import Optional

from ytmusicapi import YTMusic

from billboard_sync.matcher import SearchResult


class YTMusicAuthError(Exception):
    pass


class YTMusicAPIError(Exception):
    pass


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("unauthorized", "401", "403", "forbidden", "auth", "cookie"))


def _parse_search_result(item: dict) -> Optional[SearchResult]:
    """Convert one raw `ytmusicapi.YTMusic.search(...)` result dict into a `SearchResult`.

    `item` is one element of the list returned by `ytmusicapi`'s search call. With
    `filter="songs"` each item is shaped roughly like:
        {"videoId": "rW2HmFDGdKs",
         "title":   "Golden",
         "artists": [{"name": "HUNTR/X", "id": "..."}, ...],
         "resultType": "song",   # or "video"/"album"/"artist" if the filter leaks
         "album": {...}, "duration": "3:21", ...}
    We only care about the four fields above; everything else is dropped. Returns
    None if `videoId` is missing (an unplayable entry — e.g. an album header row).
    """
    video_id = item.get("videoId")
    if not video_id:
        return None
    artists_raw = item.get("artists") or []
    artists = [a.get("name", "") for a in artists_raw if isinstance(a, dict)]
    kind = (item.get("resultType") or item.get("category") or "").lower()
    return SearchResult(
        video_id=video_id,
        title=item.get("title", ""),
        artists=artists,
        kind=kind,
    )


class YTMusicClient:
    def __init__(self, auth_file: str) -> None:
        try:
            self._yt = YTMusic(auth_file)
        except Exception as exc:
            raise YTMusicAuthError(str(exc)) from exc

    def search_songs(self, query: str, limit: int = 5) -> list[SearchResult]:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                raw = self._yt.search(query, filter="songs", limit=limit)
                results: list[SearchResult] = []
                for item in raw[:limit]:
                    parsed = _parse_search_result(item)
                    if parsed:
                        results.append(parsed)
                return results
            except Exception as exc:
                if _is_auth_error(exc):
                    raise YTMusicAuthError(str(exc)) from exc
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise YTMusicAPIError(f"Search failed for {query!r}: {last_exc}")

    def clear_playlist(self, playlist_id: str) -> None:
        """Remove every track from the playlist."""
        try:
            data = self._yt.get_playlist(playlist_id, limit=10000)
        except Exception as exc:
            if _is_auth_error(exc):
                raise YTMusicAuthError(str(exc)) from exc
            raise YTMusicAPIError(f"get_playlist failed: {exc}") from exc

        items = [
            {"videoId": tr["videoId"], "setVideoId": tr["setVideoId"]}
            for tr in (data.get("tracks") or [])
            if tr.get("videoId") and tr.get("setVideoId")
        ]
        if not items:
            return

        try:
            self._yt.remove_playlist_items(playlist_id, items)
        except Exception as exc:
            if _is_auth_error(exc):
                raise YTMusicAuthError(str(exc)) from exc
            raise YTMusicAPIError(f"remove_playlist_items failed: {exc}") from exc

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> None:
        if not video_ids:
            return
        try:
            self._yt.add_playlist_items(playlist_id, video_ids)
        except Exception as exc:
            if _is_auth_error(exc):
                raise YTMusicAuthError(str(exc)) from exc
            raise YTMusicAPIError(f"add_playlist_items failed: {exc}") from exc
