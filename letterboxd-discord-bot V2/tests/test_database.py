from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from letterboxd_bot.database import Database
from letterboxd_bot.models import FeedEntry


@pytest.mark.asyncio
async def test_subscription_and_deduplication(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    await database.initialize()

    created = await database.upsert_subscription(
        1,
        "movie-fan",
        10,
        100,
        track_all=False,
        high_threshold=4.0,
        low_threshold=1.0,
    )
    assert created is True
    assert await database.subscription_count(1) == 1

    await database.seed_processed_entries(1, "movie-fan", ["entry-1", "entry-2"])
    assert await database.is_processed(1, "movie-fan", "entry-1") is True
    assert await database.is_processed(1, "movie-fan", "entry-3") is False

    await database.record_entries(
        "movie-fan",
        [
            FeedEntry(
                entry_id="entry-1",
                film_title="A Great Film",
                film_year=2024,
                rating=4.5,
                link="https://letterboxd.com/movie-fan/film/a-great-film/",
                watched_date=date(2026, 6, 22),
                published_at=datetime(2026, 6, 22, 12, tzinfo=timezone.utc),
                tmdb_id=10,
            ),
            FeedEntry(
                entry_id="entry-2",
                film_title="A Rough Film",
                film_year=2022,
                rating=1.0,
                link="https://letterboxd.com/movie-fan/film/a-rough-film/",
                watched_date=date(2026, 6, 21),
                published_at=datetime(2026, 6, 21, 12, tzinfo=timezone.utc),
                tmdb_id=20,
            ),
        ],
    )
    context = await database.get_profile_context(
        "movie-fan", limit=100, high_threshold=4.0, low_threshold=1.0
    )
    assert context is not None
    assert context["total_observed_ratings"] == 2
    assert context["average_rating"] == 2.75
    assert context["high_rating_count"] == 1
    assert context["low_rating_count"] == 1
    assert context["rating_distribution"] == {"1.0": 1, "4.5": 1}

    records = await database.get_rating_records(["movie-fan"])
    assert len(records) == 2
    assert records[0].film_title == "A Great Film"
    assert records[0].tmdb_id == 10

    assert await database.consume_gemini_quota(1, daily_limit=1) is True
    assert await database.consume_gemini_quota(1, daily_limit=1) is False

    created_again = await database.upsert_subscription(
        1,
        "movie-fan",
        11,
        101,
        track_all=True,
        high_threshold=4.5,
        low_threshold=0.5,
    )
    assert created_again is False
    subscription = await database.get_subscription(1, "movie-fan")
    assert subscription is not None
    assert subscription.channel_id == 11
    assert subscription.track_all is True
    assert subscription.high_threshold == 4.5
    assert subscription.low_threshold == 0.5

    await database.replace_all_time_ratings(
        guild_id=1,
        username="movie-fan",
        uploaded_by=100,
        source_filename="export.zip",
        records=records,
    )
    assert await database.get_all_time_import_owner(1, "movie-fan") == 100
    assert await database.list_all_time_usernames(1) == ["movie-fan"]
    all_time = await database.get_all_time_rating_records(1, ["movie-fan"])
    assert len(all_time) == 2
    assert all_time[0].film_title == "A Great Film"

    assert await database.remove_subscription(1, "movie-fan") is True
    assert await database.subscription_count(1) == 0
    assert await database.delete_all_time_import(1, "movie-fan") is True
    assert await database.get_all_time_rating_records(1, ["movie-fan"]) == []
