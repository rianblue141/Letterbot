import io
import zipfile

import pytest

from letterboxd_bot.exports import ExportError, parse_letterboxd_export


def _make_export(*, include_ratings: bool = True) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "profile.csv",
            'Date Joined,Username,Given Name,Family Name,Email Address,Bio\n'
            '2024-01-01,movie-fan,Movie,Fan,private@example.com,"multiline\n bio"\n',
        )
        if include_ratings:
            archive.writestr(
                "ratings.csv",
                "Date,Name,Year,Letterboxd URI,Rating\n"
                "2024-01-01,Great Film,2023,https://boxd.it/abc,4.5\n"
                "2024-01-02,Bad Film,2022,https://boxd.it/def,1\n",
            )
    return output.getvalue()


def test_parse_letterboxd_export_reads_only_rating_data() -> None:
    parsed = parse_letterboxd_export(_make_export())

    assert parsed.username == "movie-fan"
    assert len(parsed.ratings) == 2
    assert parsed.ratings[0].film_title == "Great Film"
    assert parsed.ratings[0].rating == 4.5
    assert parsed.ratings[0].watched_date == "2024-01-01"


def test_parse_letterboxd_export_requires_ratings_csv() -> None:
    with pytest.raises(ExportError):
        parse_letterboxd_export(_make_export(include_ratings=False))
