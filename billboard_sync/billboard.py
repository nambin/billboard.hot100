from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

CHART_URL = "https://www.billboard.com/charts/hot-100/"
USER_AGENT = "Mozilla/5.0 (compatible; billboard-sync/0.1)"


@dataclass(frozen=True)
class ChartEntry:
    rank: int
    title: str
    artist: str


class BillboardParseError(Exception):
    pass


def fetch_billboard_html(timeout: int = 15, retries: int = 3) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = requests.get(
                CHART_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise BillboardParseError(f"Failed to fetch Billboard chart: {last_exc}")


def parse_chart_date(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    t = soup.find("time")
    if t and t.get("datetime"):
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            pass
    for a in soup.find_all("a", href=True):
        m = re.search(r"/(\d{4}-\d{2}-\d{2})/", a["href"])
        if m:
            return m.group(1)
    return None


_BADGE_LABELS = {"NEW", "RE-ENTRY", "RE", "HOT SHOT DEBUT"}


def _extract_artist(row) -> str:
    """Pull the artist string from a chart row.

    Preferred path: `.c-label.a-no-trucate` is Billboard's artist-label class
    (note: their HTML misspells 'truncate'). Fallback scans `.c-label` and
    skips ranks, dashes, and known badge labels — kept narrow so a future
    class rename surfaces as a parse error rather than wrong data.
    """
    el = row.select_one(".c-label.a-no-trucate")
    if el:
        return el.get_text(separator=" ", strip=True)

    for label in row.select(".c-label"):
        text = label.get_text(separator=" ", strip=True)
        if not text or text == "-" or text.isdigit() or text.upper() in _BADGE_LABELS:
            continue
        return text
    return ""


def parse_billboard_hot_100(html: str, top_n: int) -> list[ChartEntry]:
    if not 1 <= top_n <= 100:
        raise ValueError(f"top_n must be in 1..100, got {top_n}")

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(".o-chart-results-list-row-container")

    entries: list[ChartEntry] = []
    for row in rows:
        title_el = row.select_one(".c-title")
        if not title_el:
            continue
        title = title_el.get_text(separator=" ", strip=True)
        artist = _extract_artist(row)

        if not title or not artist:
            continue

        rank = len(entries) + 1
        entries.append(ChartEntry(rank=rank, title=title, artist=artist))
        if len(entries) >= top_n:
            break

    if len(entries) < top_n:
        raise BillboardParseError(
            f"Expected {top_n} chart entries, parsed {len(entries)}. "
            "Billboard's HTML may have changed; update billboard_sync.billboard."
        )
    return entries
