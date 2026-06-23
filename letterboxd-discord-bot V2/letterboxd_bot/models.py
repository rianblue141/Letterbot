from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class FeedEntry:
    entry_id: str
    film_title: str
    film_year: int | None
    rating: float | None
    link: str
    watched_date: date | None
    published_at: datetime | None
    tmdb_id: int | None = None


@dataclass(frozen=True, slots=True)
class Subscription:
    guild_id: int
    username: str
    channel_id: int
    created_by: int
    track_all: bool
    high_threshold: float
    low_threshold: float


@dataclass(frozen=True, slots=True)
class RatingRecord:
    username: str
    film_title: str
    film_year: int | None
    rating: float
    link: str
    watched_date: str | None
    published_at: str | None
    tmdb_id: int | None
