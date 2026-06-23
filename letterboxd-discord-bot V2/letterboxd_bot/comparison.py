from __future__ import annotations

import re
from dataclasses import dataclass

from .models import RatingRecord
from .rss import normalize_username
from .summary import RatingSummary, calculate_rating_summary


MAX_COMPARE_USERS = 50


@dataclass(frozen=True, slots=True)
class UserComparison:
    username: str
    summary: RatingSummary


def parse_username_list(value: str) -> list[str]:
    raw_names = [part for part in re.split(r"[,;\s]+", value.strip()) if part]
    if len(raw_names) < 2:
        raise ValueError("Provide at least two Letterboxd usernames.")
    usernames: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        username = normalize_username(raw_name)
        if username not in seen:
            seen.add(username)
            usernames.append(username)
    if len(usernames) < 2:
        raise ValueError("Provide at least two different Letterboxd usernames.")
    if len(usernames) > MAX_COMPARE_USERS:
        raise ValueError(f"Compare at most {MAX_COMPARE_USERS} users at once.")
    return usernames


def calculate_user_comparisons(
    records: list[RatingRecord], usernames: list[str]
) -> list[UserComparison]:
    by_username: dict[str, list[RatingRecord]] = {username: [] for username in usernames}
    for record in records:
        if record.username in by_username:
            by_username[record.username].append(record)

    comparisons: list[UserComparison] = []
    for username in usernames:
        summary = calculate_rating_summary(by_username[username])
        if summary is not None:
            comparisons.append(UserComparison(username=username, summary=summary))
    return comparisons
