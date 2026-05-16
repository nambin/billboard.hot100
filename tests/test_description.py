from billboard_sync.billboard_to_ytmusic_sync import (
    _build_description,
    _format_human_date,
    _ordinal_suffix,
)


def test_ordinal_suffix():
    assert _ordinal_suffix(1) == "st"
    assert _ordinal_suffix(2) == "nd"
    assert _ordinal_suffix(3) == "rd"
    assert _ordinal_suffix(4) == "th"
    assert _ordinal_suffix(9) == "th"
    # 11/12/13 are exceptions — always "th".
    assert _ordinal_suffix(11) == "th"
    assert _ordinal_suffix(12) == "th"
    assert _ordinal_suffix(13) == "th"
    # 21/22/23 follow the regular rule again.
    assert _ordinal_suffix(21) == "st"
    assert _ordinal_suffix(22) == "nd"
    assert _ordinal_suffix(23) == "rd"
    assert _ordinal_suffix(31) == "st"


def test_format_human_date():
    assert _format_human_date("2026-05-09") == "May 9th, 2026"
    assert _format_human_date("2026-05-01") == "May 1st, 2026"
    assert _format_human_date("2026-12-22") == "December 22nd, 2026"
    assert _format_human_date("2026-11-13") == "November 13th, 2026"


def test_build_description():
    assert _build_description("2026-05-09", 30, "2026-05-19") == (
        "Billboard Hot 100 top 30.\n"
        "Chart week of May 9th, 2026.\n"
        "Synced May 19th, 2026."
    )
