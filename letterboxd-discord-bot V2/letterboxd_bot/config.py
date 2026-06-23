from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    database_path: Path
    poll_interval_seconds: int = 300
    high_rating_threshold: float = 4.0
    low_rating_threshold: float = 1.0
    dev_guild_id: int | None = None
    max_tracked_profiles_per_guild: int = 100
    http_timeout_seconds: int = 20
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: int = 30
    gemini_max_entries: int = 100
    gemini_question_max_chars: int = 500
    gemini_cooldown_seconds: int = 20
    gemini_daily_guild_limit: int = 100
    tmdb_read_access_token: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "replace_me":
            raise ValueError("DISCORD_TOKEN is missing. Copy .env.example to .env and add your token.")

        dev_guild = os.getenv("DEV_GUILD_ID", "").strip()
        config = cls(
            discord_token=token,
            database_path=Path(
                os.getenv("DATABASE_PATH", "data/letterboxd_bot.sqlite3")
            ).expanduser(),
            poll_interval_seconds=_int_env("POLL_INTERVAL_SECONDS", 300),
            high_rating_threshold=_float_env("HIGH_RATING_THRESHOLD", 4.0),
            low_rating_threshold=_float_env("LOW_RATING_THRESHOLD", 1.0),
            dev_guild_id=int(dev_guild) if dev_guild else None,
            max_tracked_profiles_per_guild=_int_env(
                "MAX_TRACKED_PROFILES_PER_GUILD", 100
            ),
            http_timeout_seconds=_int_env("HTTP_TIMEOUT_SECONDS", 20),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            gemini_timeout_seconds=_int_env("GEMINI_TIMEOUT_SECONDS", 30),
            gemini_max_entries=_int_env("GEMINI_MAX_ENTRIES", 100),
            gemini_question_max_chars=_int_env("GEMINI_QUESTION_MAX_CHARS", 500),
            gemini_cooldown_seconds=_int_env("GEMINI_COOLDOWN_SECONDS", 20),
            gemini_daily_guild_limit=_int_env("GEMINI_DAILY_GUILD_LIMIT", 100),
            tmdb_read_access_token=os.getenv(
                "TMDB_READ_ACCESS_TOKEN", ""
            ).strip()
            or None,
        )
        config._validate()
        return config

    def _validate(self) -> None:
        if self.poll_interval_seconds < 60:
            raise ValueError("POLL_INTERVAL_SECONDS must be at least 60.")
        if not 0.5 <= self.low_rating_threshold <= 5.0:
            raise ValueError("LOW_RATING_THRESHOLD must be between 0.5 and 5.0.")
        if not 0.5 <= self.high_rating_threshold <= 5.0:
            raise ValueError("HIGH_RATING_THRESHOLD must be between 0.5 and 5.0.")
        if self.low_rating_threshold >= self.high_rating_threshold:
            raise ValueError("LOW_RATING_THRESHOLD must be below HIGH_RATING_THRESHOLD.")
        if self.max_tracked_profiles_per_guild < 1:
            raise ValueError("MAX_TRACKED_PROFILES_PER_GUILD must be positive.")
        if self.http_timeout_seconds < 5:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be at least 5.")
        if not self.gemini_model:
            raise ValueError("GEMINI_MODEL cannot be blank.")
        if not 5 <= self.gemini_timeout_seconds <= 120:
            raise ValueError("GEMINI_TIMEOUT_SECONDS must be between 5 and 120.")
        if not 1 <= self.gemini_max_entries <= 250:
            raise ValueError("GEMINI_MAX_ENTRIES must be between 1 and 250.")
        if not 50 <= self.gemini_question_max_chars <= 1500:
            raise ValueError("GEMINI_QUESTION_MAX_CHARS must be between 50 and 1500.")
        if not 1 <= self.gemini_cooldown_seconds <= 3600:
            raise ValueError("GEMINI_COOLDOWN_SECONDS must be between 1 and 3600.")
        if not 1 <= self.gemini_daily_guild_limit <= 10000:
            raise ValueError("GEMINI_DAILY_GUILD_LIMIT must be between 1 and 10000.")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, not {raw!r}.") from exc


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, not {raw!r}.") from exc
