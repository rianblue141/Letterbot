from letterboxd_bot.metadata import parse_movie_metadata


def test_parse_movie_metadata() -> None:
    metadata = parse_movie_metadata(
        {
            "id": 10,
            "title": "Example",
            "release_date": "2024-03-08",
            "runtime": 121,
            "genres": [{"id": 1, "name": "Drama"}],
            "credits": {
                "crew": [
                    {"job": "Director", "name": "Director Name"},
                    {"job": "Writer", "name": "Writer Name"},
                ],
                "cast": [
                    {"order": 1, "name": "Second Actor"},
                    {"order": 0, "name": "First Actor"},
                ],
            },
        }
    )

    assert metadata is not None
    assert metadata.release_year == 2024
    assert metadata.directors == ("Director Name",)
    assert metadata.actors == ("First Actor", "Second Actor")
    assert metadata.genres == ("Drama",)
    assert metadata.runtime_minutes == 121
