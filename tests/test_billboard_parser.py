"""Parser tests.

To add coverage for another Billboard week:
  1. Save the snapshot HTML to tests/fixtures/billboard_sample_YYYYMMDD.html
  2. Append a `ChartFixture(...)` entry to ALL_FIXTURES below
The parametrized tests automatically run against every fixture in the list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from billboard_sync.billboard import (
    BillboardParseError,
    parse_billboard_hot_100,
    parse_chart_date,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class ChartFixture:
    """One chart-week snapshot and its expected parse output."""
    name: str
    filename: str
    chart_date: str
    expected_top_30: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return FIXTURES_DIR / self.filename

    def read_html(self) -> str:
        return self.path.read_text(encoding="utf-8")


FIXTURE_20260509 = ChartFixture(
    name="2026-05-09",
    filename="billboard_sample_20260509.html",
    chart_date="2026-05-09",
    expected_top_30=[
        ( 1, "Choosin' Texas",                       "Ella Langley"),
        ( 2, "I Just Might",                         "Bruno Mars"),
        ( 3, "Man I Need",                           "Olivia Dean"),
        ( 4, "Drop Dead",                            "Olivia Rodrigo"),
        ( 5, "Be Her",                               "Ella Langley"),
        ( 6, "So Easy (To Fall In Love)",            "Olivia Dean"),
        ( 7, "I Can't Love You Anymore",             "Ella Langley & Morgan Wallen"),
        ( 8, "Ordinary",                             "Alex Warren"),
        ( 9, "Doors",                                "Noah Kahan"),
        (10, "Folded",                               "Kehlani"),
        (11, "The Great Divide",                     "Noah Kahan"),
        (12, "Daisies",                              "Justin Bieber"),
        (13, "Golden",                               "HUNTR/X: EJAE, Audrey Nuna & REI AMI"),
        (14, "End Of August",                        "Noah Kahan"),
        (15, "Stateside",                            "PinkPantheress With Zara Larsson"),
        (16, "American Cars",                        "Noah Kahan"),
        (17, "The Fate Of Ophelia",                  "Taylor Swift"),
        (18, "Dracula",                              "Tame Impala & JENNIE"),
        (19, "Dashboard",                            "Noah Kahan"),
        (20, "Risk It All",                          "Bruno Mars"),
        (21, "Porch Light",                          "Noah Kahan"),
        (22, "Sleepless In A Hotel Room",            "Luke Combs"),
        (23, "Downfall",                             "Noah Kahan"),
        (24, "Yukon",                                "Justin Bieber"),
        (25, "Opalite",                              "Taylor Swift"),
        (26, "Willing And Able",                     "Noah Kahan"),
        (27, "Babydoll",                             "Dominic Fike"),
        (28, "Haircut",                              "Noah Kahan"),
        (29, "Paid Time Off",                        "Noah Kahan"),
        (30, "Homewrecker",                          "sombr"),
    ],
)


ALL_FIXTURES: list[ChartFixture] = [
    FIXTURE_20260509,
]


@pytest.fixture(params=ALL_FIXTURES, ids=lambda f: f.name)
def chart(request) -> ChartFixture:
    return request.param


# ---------- Per-fixture tests (run once per ChartFixture) ----------


def test_returns_exactly_top_30(chart: ChartFixture):
    entries = parse_billboard_hot_100(chart.read_html(), 30)
    assert len(entries) == 30


def test_top_n_limit_is_respected(chart: ChartFixture):
    entries = parse_billboard_hot_100(chart.read_html(), 15)
    assert len(entries) == 15
    assert entries[-1].rank == 15


def test_rank_title_artist_match_expected(chart: ChartFixture):
    if not chart.expected_top_30:
        pytest.skip(f"{chart.name}: no expected_top_30 declared")
    entries = parse_billboard_hot_100(chart.read_html(), 30)
    actual = [(e.rank, e.title, e.artist) for e in entries]
    assert actual == chart.expected_top_30


def test_parse_chart_date_matches(chart: ChartFixture):
    assert parse_chart_date(chart.read_html()) == chart.chart_date


# ---------- Fixture-independent tests ----------


def test_top_n_validation():
    html = ALL_FIXTURES[0].read_html()
    with pytest.raises(ValueError):
        parse_billboard_hot_100(html, 0)
    with pytest.raises(ValueError):
        parse_billboard_hot_100(html, 101)


def test_parser_raises_on_too_few_entries():
    minimal_html = "<html><body><div>nothing chart-shaped here</div></body></html>"
    with pytest.raises(BillboardParseError):
        parse_billboard_hot_100(minimal_html, 30)


def test_new_badge_is_skipped_and_whitespace_preserved():
    # Regression: Billboard puts a NEW debut-entry badge as a `.c-label` ahead of
    # the artist label, and renders artists like "X & Y" with the ampersand text
    # adjacent to inline anchors. The parser must (a) ignore the badge and
    # (b) join inner text with whitespace so "X &Y" doesn't collapse.
    html = """
    <html><body>
    <div class="o-chart-results-list-row-container">
      <span class="c-label">7</span>
      <h3 class="c-title">Some Song</h3>
      <span class="c-label lrv-u-background-color-brand-primary">NEW</span>
      <span class="c-label a-no-trucate"><a>Ella Langley</a> &amp;<a>Morgan Wallen</a></span>
      <span class="c-label">-</span>
    </div>
    """ * 30
    entries = parse_billboard_hot_100(html, 30)
    assert len(entries) == 30
    assert entries[0].title == "Some Song"
    assert entries[0].artist == "Ella Langley & Morgan Wallen"
