from billboard_sync.matcher import SearchResult, primary_artist, validate_match


def _result(title="Luther", artists=None, kind="song", video_id="X"):
    return SearchResult(
        video_id=video_id,
        title=title,
        artists=artists if artists is not None else ["Kendrick Lamar"],
        kind=kind,
    )


def test_song_kind_required():
    ok, _ = validate_match("Luther", "Kendrick Lamar", _result(kind="video"))
    assert not ok
    ok, _ = validate_match("Luther", "Kendrick Lamar", _result(kind="album"))
    assert not ok


def test_exact_match_passes():
    ok, score = validate_match("Luther", "Kendrick Lamar & SZA", _result())
    assert ok
    assert score >= 0.7


def test_artist_mismatch_fails():
    ok, _ = validate_match("Luther", "Kendrick Lamar", _result(artists=["Some Other Artist"]))
    assert not ok


def test_title_substring_passes():
    ok, _ = validate_match("Luther", "Kendrick Lamar", _result(title="Luther (Extended Mix)"))
    assert ok


def test_low_title_similarity_fails():
    ok, _ = validate_match("Luther", "Kendrick Lamar", _result(title="Some Unrelated Track Name"))
    assert not ok


def test_primary_artist_extraction():
    assert primary_artist("Kendrick Lamar & SZA") == "Kendrick Lamar"
    assert primary_artist("A Featuring B") == "A"
    assert primary_artist("A feat. B") == "A"
    assert primary_artist("A, B") == "A"
    assert primary_artist("Solo Artist") == "Solo Artist"


def test_primary_artist_substring_match():
    # Result lists "Kendrick Lamar, SZA" — primary "Kendrick Lamar" should be found.
    ok, _ = validate_match(
        "Luther",
        "Kendrick Lamar & SZA",
        _result(artists=["Kendrick Lamar, SZA"]),
    )
    assert ok
