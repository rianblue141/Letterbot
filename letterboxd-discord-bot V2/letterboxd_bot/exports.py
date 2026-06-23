from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

from .models import RatingRecord
from .rss import normalize_username


MAX_EXPORT_ZIP_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 250
MAX_RATING_ROWS = 100_000
REQUIRED_RATING_COLUMNS = {
    "Date",
    "Name",
    "Year",
    "Letterboxd URI",
    "Rating",
}


class ExportError(ValueError):
    """A Letterboxd export is missing, malformed, or unsafe to process."""


@dataclass(frozen=True, slots=True)
class ParsedLetterboxdExport:
    username: str
    ratings: list[RatingRecord]


def parse_letterboxd_export(data: bytes) -> ParsedLetterboxdExport:
    if not data:
        raise ExportError("The uploaded export is empty.")
    if len(data) > MAX_EXPORT_ZIP_BYTES:
        raise ExportError("The Letterboxd export ZIP is too large.")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            _validate_archive(members)
            by_name = {
                _normalized_member_name(member.filename): member for member in members
            }
            profile_info = by_name.get("profile.csv")
            ratings_info = by_name.get("ratings.csv")
            if profile_info is None or ratings_info is None:
                raise ExportError(
                    "The ZIP must contain Letterboxd's `profile.csv` and `ratings.csv`."
                )
            username = _read_username(archive, profile_info)
            ratings = _read_ratings(archive, ratings_info, username)
    except ExportError:
        raise
    except (zipfile.BadZipFile, UnicodeDecodeError, csv.Error, OSError) as exc:
        raise ExportError("The uploaded file is not a valid Letterboxd export ZIP.") from exc

    if not ratings:
        raise ExportError("The export does not contain any rated films.")
    return ParsedLetterboxdExport(username=username, ratings=ratings)


def _validate_archive(members: list[zipfile.ZipInfo]) -> None:
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ExportError("The export ZIP contains too many files.")
    total_size = 0
    seen_names: set[str] = set()
    for member in members:
        name = _normalized_member_name(member.filename)
        if not name or name.startswith("/") or ".." in name.split("/"):
            raise ExportError("The export ZIP contains an unsafe file path.")
        if member.flag_bits & 0x1:
            raise ExportError("Encrypted ZIP members are not supported.")
        if name in seen_names:
            raise ExportError("The export ZIP contains duplicate file names.")
        seen_names.add(name)
        total_size += member.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ExportError("The uncompressed Letterboxd export is too large.")


def _normalized_member_name(value: str) -> str:
    return value.replace("\\", "/").removeprefix("./").casefold()


def _read_username(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    with archive.open(info) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text)
            row = next(reader, None)
    if row is None or not row.get("Username"):
        raise ExportError("`profile.csv` does not contain a Letterboxd username.")
    try:
        return normalize_username(row["Username"])
    except ValueError as exc:
        raise ExportError("`profile.csv` contains an invalid Letterboxd username.") from exc


def _read_ratings(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, username: str
) -> list[RatingRecord]:
    ratings_by_uri: dict[str, RatingRecord] = {}
    with archive.open(info) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            reader = csv.DictReader(text)
            columns = set(reader.fieldnames or [])
            if not REQUIRED_RATING_COLUMNS.issubset(columns):
                raise ExportError("`ratings.csv` has an unexpected column layout.")
            for row_number, row in enumerate(reader, start=2):
                if row_number > MAX_RATING_ROWS + 1:
                    raise ExportError("`ratings.csv` contains too many rows.")
                record = _parse_rating_row(row, row_number, username)
                ratings_by_uri[record.link] = record
    return list(ratings_by_uri.values())


def _parse_rating_row(
    row: dict[str, str | None], row_number: int, username: str
) -> RatingRecord:
    title = (row.get("Name") or "").strip()
    uri = (row.get("Letterboxd URI") or "").strip()
    if not title or not _safe_letterboxd_export_uri(uri):
        raise ExportError(f"Invalid film data in `ratings.csv` row {row_number}.")
    try:
        rating = float((row.get("Rating") or "").strip())
    except ValueError as exc:
        raise ExportError(f"Invalid rating in `ratings.csv` row {row_number}.") from exc
    if not 0.5 <= rating <= 5.0 or abs(rating * 2 - round(rating * 2)) > 1e-9:
        raise ExportError(f"Invalid rating in `ratings.csv` row {row_number}.")

    year_value = (row.get("Year") or "").strip()
    try:
        film_year = int(year_value) if year_value else None
    except ValueError:
        film_year = None
    if film_year is not None and not 1870 <= film_year <= 2200:
        film_year = None

    date_value = (row.get("Date") or "").strip()
    try:
        rated_date = date.fromisoformat(date_value).isoformat() if date_value else None
    except ValueError:
        rated_date = None
    return RatingRecord(
        username=username,
        film_title=title[:300],
        film_year=film_year,
        rating=rating,
        link=uri[:1000],
        watched_date=rated_date,
        published_at=None,
        tmdb_id=None,
    )


def _safe_letterboxd_export_uri(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in {
        "boxd.it",
        "www.boxd.it",
        "letterboxd.com",
        "www.letterboxd.com",
    }
