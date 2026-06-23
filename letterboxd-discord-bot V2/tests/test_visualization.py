from letterboxd_bot.models import RatingRecord
from letterboxd_bot.summary import calculate_rating_summary
from letterboxd_bot.visualization import render_ratings_chart


def _record(username: str, title: str, rating: float, watched: str) -> RatingRecord:
    return RatingRecord(
        username=username,
        film_title=title,
        film_year=2024,
        rating=rating,
        link="https://letterboxd.com/example/film/example/",
        watched_date=watched,
        published_at=None,
        tmdb_id=1,
    )


def test_render_single_user_chart_as_png() -> None:
    records = [
        _record("alice", "First", 4.5, "2026-01-01"),
        _record("alice", "Second", 2.5, "2026-02-01"),
    ]
    summary = calculate_rating_summary(records)
    assert summary is not None

    image = render_ratings_chart(
        records, summary, title="Alice ratings", combined=False
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 1_000


def test_render_combined_chart_as_png() -> None:
    records = [
        _record("alice", "First", 4.5, "2026-01-01"),
        _record("bob", "Second", 1.0, "2026-02-01"),
    ]
    summary = calculate_rating_summary(records)
    assert summary is not None

    image = render_ratings_chart(
        records, summary, title="Combined ratings", combined=True
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
