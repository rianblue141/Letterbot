import pytest

from letterboxd_bot.comparison import (
    calculate_user_comparisons,
    parse_username_list,
)
from letterboxd_bot.models import RatingRecord


def _record(username: str, rating: float) -> RatingRecord:
    return RatingRecord(
        username=username,
        film_title=f"{username} film",
        film_year=2024,
        rating=rating,
        link="https://boxd.it/example",
        watched_date="2026-01-01",
        published_at=None,
        tmdb_id=None,
    )


def test_parse_username_list_accepts_commas_spaces_and_deduplicates() -> None:
    assert parse_username_list("Alice, bob alice;movie-fan") == [
        "alice",
        "bob",
        "movie-fan",
    ]


def test_parse_username_list_requires_two_unique_users() -> None:
    with pytest.raises(ValueError):
        parse_username_list("alice, alice")


def test_calculate_user_comparisons() -> None:
    comparisons = calculate_user_comparisons(
        [_record("alice", 5.0), _record("bob", 1.0)], ["alice", "bob"]
    )

    assert [item.username for item in comparisons] == ["alice", "bob"]
    assert comparisons[0].summary.average_rating == 5.0
    assert comparisons[1].summary.lowest_rated.rating == 1.0
