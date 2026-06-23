from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .models import RatingRecord


@dataclass(frozen=True, slots=True)
class RatingSummary:
    total_ratings: int
    average_rating: float
    top_rated: RatingRecord
    lowest_rated: RatingRecord
    per_user_averages: dict[str, float]


def calculate_rating_summary(records: list[RatingRecord]) -> RatingSummary | None:
    if not records:
        return None

    per_user: dict[str, list[float]] = defaultdict(list)
    for record in records:
        per_user[record.username].append(record.rating)

    ratings = [record.rating for record in records]
    # Database results are newest-first. max/min preserve the first item in a tie,
    # making the selected top/lowest observation deterministic.
    top_rated = max(records, key=lambda record: record.rating)
    lowest_rated = min(records, key=lambda record: record.rating)
    return RatingSummary(
        total_ratings=len(ratings),
        average_rating=sum(ratings) / len(ratings),
        top_rated=top_rated,
        lowest_rated=lowest_rated,
        per_user_averages={
            username: sum(user_ratings) / len(user_ratings)
            for username, user_ratings in sorted(per_user.items())
        },
    )
