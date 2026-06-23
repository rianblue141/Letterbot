from letterboxd_bot.models import RatingRecord
from letterboxd_bot.summary import calculate_rating_summary


def _record(username: str, title: str, rating: float) -> RatingRecord:
    return RatingRecord(
        username=username,
        film_title=title,
        film_year=2024,
        rating=rating,
        link="https://letterboxd.com/example/film/example/",
        watched_date="2026-06-22",
        published_at=None,
        tmdb_id=1,
    )


def test_calculate_combined_summary() -> None:
    summary = calculate_rating_summary(
        [
            _record("alice", "Favorite", 5.0),
            _record("alice", "Fine", 3.0),
            _record("bob", "Disliked", 1.0),
        ]
    )

    assert summary is not None
    assert summary.total_ratings == 3
    assert summary.average_rating == 3.0
    assert summary.top_rated.film_title == "Favorite"
    assert summary.lowest_rated.film_title == "Disliked"
    assert summary.per_user_averages == {"alice": 4.0, "bob": 1.0}


def test_empty_summary() -> None:
    assert calculate_rating_summary([]) is None
