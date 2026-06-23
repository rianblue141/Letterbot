from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree

import aiohttp

from .models import FeedEntry


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
MAX_FEED_BYTES = 2 * 1024 * 1024


class FeedError(RuntimeError):
    """A Letterboxd feed could not be retrieved or understood."""


def normalize_username(value: str) -> str:
    value = value.strip()
    if value.startswith("@"):
        value = value[1:]
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError(
            "Use a Letterboxd username containing only letters, numbers, hyphens, "
            "or underscores."
        )
    return value.lower()


def feed_url(username: str) -> str:
    return f"https://letterboxd.com/{quote(username, safe='')}/rss/"


def profile_url(username: str) -> str:
    return f"https://letterboxd.com/{quote(username, safe='')}/"


class LetterboxdRSSClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def fetch(self, username: str) -> list[FeedEntry]:
        url = feed_url(username)
        try:
            async with self._session.get(url, allow_redirects=True) as response:
                if response.status == 404:
                    raise FeedError(f"Letterboxd profile `{username}` was not found.")
                if response.status != 200:
                    raise FeedError(
                        f"Letterboxd returned HTTP {response.status} for `{username}`."
                    )
                body = await _read_limited(response)
        except FeedError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise FeedError(f"Could not reach Letterboxd for `{username}`.") from exc

        try:
            return parse_feed(body)
        except (ElementTree.ParseError, ValueError) as exc:
            raise FeedError(f"Letterboxd returned an invalid RSS feed for `{username}`.") from exc


async def _read_limited(response: aiohttp.ClientResponse) -> bytes:
    declared_size = response.content_length
    if declared_size is not None and declared_size > MAX_FEED_BYTES:
        raise FeedError("The RSS response was unexpectedly large.")

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_FEED_BYTES:
            raise FeedError("The RSS response was unexpectedly large.")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_feed(xml: bytes | str) -> list[FeedEntry]:
    """Parse Letterboxd RSS without executing or expanding external entities."""
    if isinstance(xml, str):
        raw = xml.encode("utf-8")
    else:
        raw = xml
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("Feed is too large.")

    upper_prefix = raw[:4096].upper()
    if b"<!DOCTYPE" in upper_prefix or b"<!ENTITY" in upper_prefix:
        raise ValueError("DTD and entity declarations are not accepted.")

    root = ElementTree.fromstring(raw)
    entries: list[FeedEntry] = []
    for item in (node for node in root.iter() if _local_name(node.tag) == "item"):
        guid = _child_text(item, "guid")
        link = _child_text(item, "link")
        title = _child_text(item, "filmTitle") or _fallback_film_title(
            _child_text(item, "title")
        )
        year = _parse_year(_child_text(item, "filmYear"))
        rating = _parse_rating(_child_text(item, "memberRating"))
        tmdb_id = _parse_positive_int(_child_text(item, "movieId"))
        watched_date = _parse_date(_child_text(item, "watchedDate"))
        published_at = _parse_datetime(_child_text(item, "pubDate"))
        entry_id = guid or link or _stable_fallback_id(
            title, year, watched_date, published_at
        )
        entries.append(
            FeedEntry(
                entry_id=entry_id[:500],
                film_title=(title or "Unknown film")[:300],
                film_year=year,
                rating=rating,
                link=link[:1000],
                watched_date=watched_date,
                published_at=published_at,
                tmdb_id=tmdb_id,
            )
        )
    return entries


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _parse_rating(value: str) -> float | None:
    if not value:
        return None
    try:
        rating = float(value)
    except ValueError:
        return None
    if not 0.5 <= rating <= 5.0:
        return None
    return rating


def _parse_year(value: str) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1870 <= year <= 2200 else None


def _parse_positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _fallback_film_title(title: str) -> str:
    # Typical item titles look like "Film Name, 2024 - ★★★★".
    return re.sub(r",\s*\d{4}(?:\s*-.*)?$", "", title).strip()


def _stable_fallback_id(
    title: str,
    year: int | None,
    watched_date: date | None,
    published_at: datetime | None,
) -> str:
    value = f"{title}|{year}|{watched_date}|{published_at}"
    return "fallback:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def rating_stars(rating: float) -> str:
    full_stars = int(rating)
    half = "½" if rating - full_stars >= 0.5 else ""
    return "★" * full_stars + half


def is_alert_rating(
    rating: float | None, *, high_threshold: float = 4.0, low_threshold: float = 1.0
) -> bool:
    return rating is not None and (
        rating >= high_threshold or rating <= low_threshold
    )


def is_subscription_alert_rating(
    rating: float | None,
    *,
    track_all: bool,
    high_threshold: float,
    low_threshold: float,
) -> bool:
    return rating is not None and (
        track_all
        or is_alert_rating(
            rating,
            high_threshold=high_threshold,
            low_threshold=low_threshold,
        )
    )
