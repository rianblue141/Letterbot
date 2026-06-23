from datetime import date
from pathlib import Path

import pytest

from letterboxd_bot.rss import (
    is_alert_rating,
    is_subscription_alert_rating,
    normalize_username,
    parse_feed,
    rating_stars,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_rss.xml"


def test_parse_letterboxd_feed() -> None:
    entries = parse_feed(FIXTURE.read_bytes())

    assert len(entries) == 4
    assert entries[0].entry_id == "letterboxd-watch-100"
    assert entries[0].film_title == "Perfect Days"
    assert entries[0].film_year == 2023
    assert entries[0].rating == 4.5
    assert entries[0].tmdb_id == 976893
    assert entries[0].watched_date == date(2026, 6, 22)
    assert entries[2].rating == 0.5
    assert entries[3].rating is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Example_User", "example_user"), ("@movie-fan", "movie-fan")],
)
def test_normalize_username(value: str, expected: str) -> None:
    assert normalize_username(value) == expected


@pytest.mark.parametrize("value", ["", "a", "profile/name", "https://letterboxd.com/me"])
def test_invalid_username(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_username(value)


def test_rating_stars() -> None:
    assert rating_stars(0.5) == "½"
    assert rating_stars(4.0) == "★★★★"
    assert rating_stars(4.5) == "★★★★½"


@pytest.mark.parametrize(
    ("rating", "expected"),
    [
        (None, False),
        (0.5, True),
        (1.0, True),
        (1.5, False),
        (3.5, False),
        (4.0, True),
        (5.0, True),
    ],
)
def test_alert_boundaries(rating: float | None, expected: bool) -> None:
    assert is_alert_rating(rating) is expected


def test_subscription_alert_modes_and_custom_thresholds() -> None:
    assert (
        is_subscription_alert_rating(
            2.5, track_all=True, high_threshold=4.5, low_threshold=0.5
        )
        is True
    )
    assert (
        is_subscription_alert_rating(
            3.5, track_all=False, high_threshold=3.5, low_threshold=1.5
        )
        is True
    )
    assert (
        is_subscription_alert_rating(
            2.5, track_all=False, high_threshold=3.5, low_threshold=1.5
        )
        is False
    )
