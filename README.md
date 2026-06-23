# Letterbot
A Discord bot that watches public Letterboxd RSS feeds, posts an alert when a tracked profile gives a film 4 stars or higher or 1 star or lower, summarizes stored ratings with optional TMDB movie details, and optionally uses Gemini to answer grounded questions.


Commands:

/track username [channel] - starts tracking a profile (Manage Server required)

/untrack username - stops tracking a profile (Manage Server required)

/tracked - lists this server's tracked profiles

/check-now - runs an immediate RSS check (Manage Server required)

/ask username question - asks Gemini about ratings observed for a tracked profile

/summary user username - summarizes one profile with a distribution and timeline chart

/summary all - combines every tracked profile with distribution and per-user charts

What it does:

Polls each unique Letterboxd feed once per interval, even if several servers track it

Stores subscriptions, rating details, usage limits, and processed RSS IDs in SQLite

Seeds current feed items when tracking starts, so old ratings are not announced

Retries an alert later if Discord could not receive it

Limits Gemini use per Discord user and per server/day
