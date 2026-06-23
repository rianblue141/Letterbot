# Letterboxd Rating Alerts for Discord

A Discord bot that watches public Letterboxd RSS feeds, posts an alert when a tracked profile gives a film **4 stars or higher** or **1 star or lower**, summarizes stored ratings with optional TMDB movie details, and optionally uses Gemini to answer grounded questions.

RSS parsing, threshold checks, deduplication, storage, averages, distributions, and counts remain deterministic Python. Gemini receives only that prepared data and turns it into a readable answer. The bot does not scrape Letterboxd profile or statistics pages, and alerts continue to work if Gemini is unavailable.

## What it does

- `/track username [channel]` - starts tracking a profile (Manage Server required)
- `/untrack username` - stops tracking a profile (Manage Server required)
- `/tracked` - lists this server's tracked profiles
- `/check-now` - runs an immediate RSS check (Manage Server required)
- `/ask username question` - asks Gemini about ratings observed for a tracked profile
- `/summary user username` - summarizes one profile with a distribution and timeline chart
- `/summary all` - combines every tracked profile with distribution and per-user charts
- Polls each unique Letterboxd feed once per interval, even if several servers track it
- Stores subscriptions, rating details, usage limits, and processed RSS IDs in SQLite
- Seeds current feed items when tracking starts, so old ratings are not announced
- Retries an alert later if Discord could not receive it
- Limits Gemini use per Discord user and per server/day

## 1. Create the Discord bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application.
2. Open **Bot**, create/reset the token, and keep it private.
3. Open **OAuth2 -> URL Generator**.
4. Select the `bot` and `applications.commands` scopes.
5. Give it these bot permissions: **View Channels**, **Send Messages**, **Embed Links**, and **Read Message History**.
6. Open the generated URL to invite it to your server.

No privileged intents or Message Content intent are needed.

## 2. Install and configure

Python 3.11 or newer is required. From this project directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set:

```dotenv
DISCORD_TOKEN=your_real_bot_token
GEMINI_API_KEY=your_new_gemini_api_key
TMDB_READ_ACCESS_TOKEN=your_tmdb_api_read_access_token
```

Create the Gemini key in [Google AI Studio](https://aistudio.google.com/app/apikey). Keep it only in `.env`; that file is excluded from Git. If a key has ever been pasted into chat, a ticket, or a commit, revoke it and create a replacement before using the bot.

`GEMINI_MODEL` defaults to `gemini-2.5-flash`. It is configurable because model availability can vary by account and change over time. Set it to a compatible model available in your Gemini API project if needed.

The summary commands work without TMDB, but director, cast, genres, runtime, and enriched release details require a [TMDB API Read Access Token](https://developer.themoviedb.org/docs/getting-started). Put the token in `TMDB_READ_ACCESS_TOKEN`; do not use the shorter v3 API key in that setting.

For development, also put your test server's numeric ID in `DEV_GUILD_ID`. Enable Discord Developer Mode, right-click the server, and choose **Copy Server ID**. Guild-scoped commands update immediately. Leave this setting blank when you want global commands; Discord may take a while to show a newly synchronized global command everywhere.

## 3. Run

```powershell
python -m letterboxd_bot
```

Then use `/track` in Discord. The default polling interval is five minutes. The first RSS snapshot is saved for analysis but not announced, so `/ask` has some data immediately without flooding the channel with old alerts.

Example questions:

```text
/ask username:movie-fan question:How generous are their observed ratings?
/ask username:movie-fan question:Which films did they rate most highly?
/ask username:movie-fan question:Has their scoring changed across the observed period?
```

Gemini is instructed to say when the RSS sample cannot support an answer. In particular, it should not claim that recent RSS entries represent a profile's complete or all-time history.

Summary examples:

```text
/summary user username:movie-fan
/summary all
```

`/summary all` calculates one combined average across all stored rating entries for all profiles tracked in the current Discord server. It also shows each profile's individual average. If several entries tie for highest or lowest, the most recent tied entry is displayed. Rewatch entries with their own RSS rating count as separate available ratings.

Every summary includes a PNG visualization generated locally by the bot:

- A single-user summary shows the rating distribution and observed rating history over time.
- The all-users summary shows the combined rating distribution and average rating by tracked user. If more than 20 profiles are tracked, the chart displays the 20 highest averages while the Discord text summary still uses all profiles and all available ratings.

## Test

```powershell
python -m pip install -r requirements-dev.txt
pytest
```

The test suite does not call Gemini or TMDB and does not require real API credentials.

## Deployment notes

- Keep the process running continuously with Docker, a service manager, or your hosting provider's worker-process feature.
- Persist both `.env` and the `data/` directory across deployments. The SQLite file is the bot's memory.
- Run only one copy of the bot against a given database. Multiple processes can race and send duplicate messages.
- Never commit `.env`, the Discord token, or the Gemini API key.
- Never commit the TMDB read access token.
- Rotate any secret immediately if it is exposed in chat, a ticket, logs, or a commit.
- The bot requests `https://letterboxd.com/{username}/rss/`. Private or nonexistent feeds cannot be tracked.

## Project layout

```text
letterboxd_bot/
  bot.py       Discord commands, polling, and embeds
  config.py    environment configuration and validation
  database.py  SQLite ratings, subscriptions, limits, and deduplication
  gemini.py    grounded Gemini prompt and asynchronous API call
  metadata.py  optional TMDB movie metadata lookup
  rss.py       bounded HTTP retrieval and RSS parsing
  summary.py   deterministic rating aggregation
  visualization.py  local PNG charts for summary commands
tests/         parser and database tests
```

## Optional configuration

See `.env.example` for every setting. Important defaults:

```dotenv
HIGH_RATING_THRESHOLD=4.0
LOW_RATING_THRESHOLD=1.0
POLL_INTERVAL_SECONDS=300
GEMINI_MODEL=gemini-2.5-flash
GEMINI_COOLDOWN_SECONDS=20
GEMINI_DAILY_GUILD_LIMIT=100
GEMINI_MAX_ENTRIES=100
TMDB_READ_ACCESS_TOKEN=
```

This product uses the TMDB API but is not endorsed or certified by TMDB.
