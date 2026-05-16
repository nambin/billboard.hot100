from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

CHART_URL = "https://www.billboard.com/charts/hot-100/"
USER_AGENT = "Mozilla/5.0 (compatible; billboard-to-ytmusic-sync/0.1)"


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


_WEEK_OF_RE = re.compile(r"Week of (\w+ \d{1,2}),? (\d{4})", re.IGNORECASE)
_CHART_HREF_RE = re.compile(r"/charts/hot-100/(\d{4}-\d{2}-\d{2})/")


def parse_chart_date(html: str) -> Optional[str]:
    """Extract the chart's issue date (YYYY-MM-DD) so the report can label the week.

    Primary: the human-readable "Week of <Month Day, Year>" header text. This is
    the only signal that names the *current* chart's date unambiguously. The page
    also has `<time>` elements but their `datetime` attrs are a Billboard template
    placeholder ("00:00-YY-DD-MM") on the live page, so we don't trust them.

    Fallback: dated hrefs, restricted to `/charts/hot-100/YYYY-MM-DD/` paths and
    filtered to dates ≤ today, picking the most recent. The naive "first dated
    href" used to land on a historical archive link buried in the page.

    Returns None if no signal works — the caller substitutes today's date.
    """
    m = _WEEK_OF_RE.search(html)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    today = date.today().isoformat()
    soup = BeautifulSoup(html, "html.parser")
    candidates = {
        href_m.group(1)
        for a in soup.find_all("a", href=True)
        if (href_m := _CHART_HREF_RE.search(a["href"])) and href_m.group(1) <= today
    }
    return max(candidates) if candidates else None


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
