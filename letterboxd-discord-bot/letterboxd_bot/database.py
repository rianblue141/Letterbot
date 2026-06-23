from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .models import FeedEntry, RatingRecord, Subscription


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS subscriptions (
    guild_id INTEGER NOT NULL,
    username TEXT NOT NULL COLLATE NOCASE,
    channel_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, username)
);

CREATE TABLE IF NOT EXISTS processed_entries (
    guild_id INTEGER NOT NULL,
    username TEXT NOT NULL COLLATE NOCASE,
    entry_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, username, entry_id),
    FOREIGN KEY (guild_id, username)
        REFERENCES subscriptions (guild_id, username)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_username
    ON subscriptions (username);

CREATE TABLE IF NOT EXISTS rss_entries (
    username TEXT NOT NULL COLLATE NOCASE,
    entry_id TEXT NOT NULL,
    film_title TEXT NOT NULL,
    film_year INTEGER,
    rating REAL,
    link TEXT NOT NULL,
    watched_date TEXT,
    published_at TEXT,
    tmdb_id INTEGER,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (username, entry_id)
);

CREATE INDEX IF NOT EXISTS idx_rss_entries_profile_date
    ON rss_entries (username, watched_date, published_at);

CREATE TABLE IF NOT EXISTS gemini_daily_usage (
    guild_id INTEGER NOT NULL,
    usage_date TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    PRIMARY KEY (guild_id, usage_date)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as connection:
            await connection.executescript(SCHEMA)
            columns_cursor = await connection.execute("PRAGMA table_info(rss_entries)")
            columns = {row[1] for row in await columns_cursor.fetchall()}
            if "tmdb_id" not in columns:
                await connection.execute(
                    "ALTER TABLE rss_entries ADD COLUMN tmdb_id INTEGER"
                )
            await connection.commit()

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path, timeout=15)

    async def upsert_subscription(
        self, guild_id: int, username: str, channel_id: int, created_by: int
    ) -> bool:
        """Create/update a subscription and return True only when newly created."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._connect() as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            cursor = await connection.execute(
                "SELECT 1 FROM subscriptions WHERE guild_id = ? AND username = ?",
                (guild_id, username),
            )
            existed = await cursor.fetchone() is not None
            await connection.execute(
                """
                INSERT INTO subscriptions
                    (guild_id, username, channel_id, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, username) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    created_by = excluded.created_by
                """,
                (guild_id, username, channel_id, created_by, now),
            )
            await connection.commit()
        return not existed

    async def remove_subscription(self, guild_id: int, username: str) -> bool:
        async with self._connect() as connection:
            await connection.execute("PRAGMA foreign_keys = ON")
            cursor = await connection.execute(
                "DELETE FROM subscriptions WHERE guild_id = ? AND username = ?",
                (guild_id, username),
            )
            await connection.commit()
            return cursor.rowcount > 0

    async def subscription_count(self, guild_id: int) -> int:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE guild_id = ?", (guild_id,)
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_subscription(
        self, guild_id: int, username: str
    ) -> Subscription | None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT guild_id, username, channel_id, created_by
                FROM subscriptions
                WHERE guild_id = ? AND username = ?
                """,
                (guild_id, username),
            )
            row = await cursor.fetchone()
        return Subscription(*row) if row else None

    async def list_subscriptions(self, guild_id: int | None = None) -> list[Subscription]:
        query = "SELECT guild_id, username, channel_id, created_by FROM subscriptions"
        params: tuple[int, ...] = ()
        if guild_id is not None:
            query += " WHERE guild_id = ?"
            params = (guild_id,)
        query += " ORDER BY username COLLATE NOCASE"
        async with self._connect() as connection:
            cursor = await connection.execute(query, params)
            rows = await cursor.fetchall()
        return [Subscription(*row) for row in rows]

    async def seed_processed_entries(
        self, guild_id: int, username: str, entry_ids: Iterable[str]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        values = [(guild_id, username, entry_id, now) for entry_id in entry_ids]
        if not values:
            return
        async with self._connect() as connection:
            await connection.executemany(
                """
                INSERT OR IGNORE INTO processed_entries
                    (guild_id, username, entry_id, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                values,
            )
            await connection.commit()

    async def is_processed(self, guild_id: int, username: str, entry_id: str) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT 1 FROM processed_entries
                WHERE guild_id = ? AND username = ? AND entry_id = ?
                """,
                (guild_id, username, entry_id),
            )
            return await cursor.fetchone() is not None

    async def mark_processed(self, guild_id: int, username: str, entry_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT OR IGNORE INTO processed_entries
                    (guild_id, username, entry_id, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, username, entry_id, now),
            )
            await connection.commit()

    async def record_entries(
        self, username: str, entries: Iterable[FeedEntry]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        values = [
            (
                username,
                entry.entry_id,
                entry.film_title,
                entry.film_year,
                entry.rating,
                entry.link,
                entry.watched_date.isoformat() if entry.watched_date else None,
                entry.published_at.isoformat() if entry.published_at else None,
                entry.tmdb_id,
                now,
            )
            for entry in entries
        ]
        if not values:
            return
        async with self._connect() as connection:
            await connection.executemany(
                """
                INSERT INTO rss_entries (
                    username, entry_id, film_title, film_year, rating, link,
                    watched_date, published_at, tmdb_id, discovered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username, entry_id) DO UPDATE SET
                    film_title = excluded.film_title,
                    film_year = excluded.film_year,
                    rating = excluded.rating,
                    link = excluded.link,
                    watched_date = excluded.watched_date,
                    published_at = excluded.published_at,
                    tmdb_id = excluded.tmdb_id
                """,
                values,
            )
            await connection.commit()

    async def get_profile_context(
        self,
        username: str,
        *,
        limit: int,
        high_threshold: float,
        low_threshold: float,
    ) -> dict[str, Any] | None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                SELECT film_title, film_year, rating, link, watched_date, published_at,
                       tmdb_id
                FROM rss_entries
                WHERE username = ? AND rating IS NOT NULL
                ORDER BY
                    COALESCE(watched_date, published_at, discovered_at) DESC,
                    discovered_at DESC
                LIMIT ?
                """,
                (username, limit),
            )
            rows = await cursor.fetchall()
            rating_cursor = await connection.execute(
                """
                SELECT rating
                FROM rss_entries
                WHERE username = ? AND rating IS NOT NULL
                """,
                (username,),
            )
            rating_rows = await rating_cursor.fetchall()

        if not rows or not rating_rows:
            return None

        ratings = [float(row[0]) for row in rating_rows]
        distribution = Counter(f"{rating:.1f}" for rating in ratings)
        observed_entries = [
            {
                "film_title": row[0],
                "film_year": row[1],
                "rating": float(row[2]),
                "watched_date": row[4],
                "published_at": row[5],
                "review_url": row[3],
                "tmdb_id": row[6],
            }
            for row in rows
        ]
        dated_entries = [
            entry["watched_date"] or entry["published_at"]
            for entry in observed_entries
            if entry["watched_date"] or entry["published_at"]
        ]
        return {
            "scope": (
                "Ratings observed through the member's public Letterboxd RSS feed; "
                "this is not guaranteed to be a complete or all-time history."
            ),
            "total_observed_ratings": len(ratings),
            "entries_in_prompt": len(observed_entries),
            "average_rating": round(sum(ratings) / len(ratings), 2),
            "highest_rating": max(ratings),
            "lowest_rating": min(ratings),
            "high_rating_count": sum(
                rating >= high_threshold for rating in ratings
            ),
            "low_rating_count": sum(rating <= low_threshold for rating in ratings),
            "rating_distribution": dict(sorted(distribution.items())),
            "observed_period": {
                "newest": max(dated_entries) if dated_entries else None,
                "oldest": min(dated_entries) if dated_entries else None,
            },
            "observed_entries": observed_entries,
        }

    async def get_rating_records(
        self, usernames: Iterable[str]
    ) -> list[RatingRecord]:
        selected = sorted(set(usernames))
        if not selected:
            return []
        placeholders = ", ".join("?" for _ in selected)
        async with self._connect() as connection:
            cursor = await connection.execute(
                f"""
                SELECT username, film_title, film_year, rating, link,
                       watched_date, published_at, tmdb_id
                FROM rss_entries
                WHERE rating IS NOT NULL
                  AND username IN ({placeholders})
                ORDER BY
                    COALESCE(watched_date, published_at, discovered_at) DESC,
                    discovered_at DESC
                """,
                tuple(selected),
            )
            rows = await cursor.fetchall()
        return [
            RatingRecord(
                username=row[0],
                film_title=row[1],
                film_year=row[2],
                rating=float(row[3]),
                link=row[4],
                watched_date=row[5],
                published_at=row[6],
                tmdb_id=row[7],
            )
            for row in rows
        ]

    async def consume_gemini_quota(self, guild_id: int, daily_limit: int) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        async with self._connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO gemini_daily_usage (guild_id, usage_date, request_count)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, usage_date) DO UPDATE SET
                    request_count = request_count + 1
                WHERE request_count < ?
                """,
                (guild_id, today, daily_limit),
            )
            await connection.commit()
            return cursor.rowcount > 0
