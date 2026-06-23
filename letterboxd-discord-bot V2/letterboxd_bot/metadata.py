from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiohttp

from .models import RatingRecord


TMDB_API_ROOT = "https://api.themoviedb.org/3"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class MetadataError(RuntimeError):
    """TMDB movie metadata could not be retrieved."""


@dataclass(frozen=True, slots=True)
class MovieMetadata:
    tmdb_id: int
    title: str
    release_year: int | None
    directors: tuple[str, ...]
    actors: tuple[str, ...]
    genres: tuple[str, ...]
    runtime_minutes: int | None


class TMDbClient:
    def __init__(self, session: aiohttp.ClientSession, read_access_token: str) -> None:
        self._session = session
        self._token = read_access_token
        self._cache: dict[str, MovieMetadata | None] = {}

    async def get_movie(self, record: RatingRecord) -> MovieMetadata | None:
        cache_key = (
            f"id:{record.tmdb_id}"
            if record.tmdb_id
            else f"search:{record.film_title.casefold()}:{record.film_year}"
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        tmdb_id = record.tmdb_id
        if tmdb_id is None:
            tmdb_id = await self._search_movie(record.film_title, record.film_year)
        if tmdb_id is None:
            self._cache[cache_key] = None
            return None

        payload = await self._request_json(
            f"/movie/{tmdb_id}", params={"append_to_response": "credits"}
        )
        metadata = parse_movie_metadata(payload) if payload else None
        self._cache[cache_key] = metadata
        if metadata is not None:
            self._cache[f"id:{metadata.tmdb_id}"] = metadata
        return metadata

    async def _search_movie(self, title: str, year: int | None) -> int | None:
        params: dict[str, str] = {"query": title, "include_adult": "false"}
        if year is not None:
            params["primary_release_year"] = str(year)
        payload = await self._request_json("/search/movie", params=params)
        results = payload.get("results", []) if payload else []
        if not isinstance(results, list):
            return None

        normalized_title = _normalize_title(title)
        for result in results:
            if not isinstance(result, dict):
                continue
            candidate_title = result.get("title") or result.get("original_title")
            candidate_year = _release_year(result.get("release_date"))
            if (
                isinstance(candidate_title, str)
                and _normalize_title(candidate_title) == normalized_title
                and (year is None or candidate_year == year)
                and isinstance(result.get("id"), int)
            ):
                return result["id"]
        return None

    async def _request_json(
        self, path: str, *, params: dict[str, str]
    ) -> dict[str, Any] | None:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "accept": "application/json",
        }
        try:
            async with self._session.get(
                f"{TMDB_API_ROOT}{path}", headers=headers, params=params
            ) as response:
                if response.status == 404:
                    return None
                if response.status in {401, 403}:
                    raise MetadataError(
                        "TMDB rejected the read access token; check the bot configuration."
                    )
                if response.status != 200:
                    raise MetadataError(f"TMDB returned HTTP {response.status}.")
                if (
                    response.content_length is not None
                    and response.content_length > MAX_RESPONSE_BYTES
                ):
                    raise MetadataError("TMDB returned an unexpectedly large response.")
                body = await response.content.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise MetadataError("TMDB returned an unexpectedly large response.")
                payload = json.loads(body)
        except MetadataError:
            raise
        except (
            aiohttp.ClientError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise MetadataError("TMDB movie metadata is currently unavailable.") from exc
        return payload if isinstance(payload, dict) else None


def parse_movie_metadata(payload: dict[str, Any]) -> MovieMetadata | None:
    tmdb_id = payload.get("id")
    title = payload.get("title") or payload.get("original_title")
    if not isinstance(tmdb_id, int) or not isinstance(title, str):
        return None

    credits = payload.get("credits")
    crew = credits.get("crew", []) if isinstance(credits, dict) else []
    cast = credits.get("cast", []) if isinstance(credits, dict) else []
    if not isinstance(crew, list):
        crew = []
    if not isinstance(cast, list):
        cast = []
    directors = tuple(
        member["name"]
        for member in crew
        if isinstance(member, dict)
        and member.get("job") == "Director"
        and isinstance(member.get("name"), str)
    )
    actors = tuple(
        member["name"]
        for member in sorted(
            (item for item in cast if isinstance(item, dict)),
            key=_cast_order,
        )[:5]
        if isinstance(member.get("name"), str)
    )
    raw_genres = payload.get("genres", [])
    if not isinstance(raw_genres, list):
        raw_genres = []
    genres = tuple(
        genre["name"]
        for genre in raw_genres
        if isinstance(genre, dict) and isinstance(genre.get("name"), str)
    )
    runtime = payload.get("runtime")
    return MovieMetadata(
        tmdb_id=tmdb_id,
        title=title,
        release_year=_release_year(payload.get("release_date")),
        directors=directors,
        actors=actors,
        genres=genres,
        runtime_minutes=runtime if isinstance(runtime, int) and runtime > 0 else None,
    )


def _normalize_title(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _cast_order(item: dict[str, Any]) -> int:
    order = item.get("order")
    return order if isinstance(order, int) else 9999


def _release_year(value: object) -> int | None:
    if not isinstance(value, str) or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None
