# Letterboxd Rating Alerts for Discord

A Discord bot that watches public Letterboxd RSS feeds, sends configurable rating alerts, imports user-provided Letterboxd exports for all-time analysis, creates rating visualizations, compares profiles, and optionally uses Gemini for grounded questions.

Deterministic Python handles RSS parsing, import validation, thresholds, deduplication, statistics, comparisons, and charts. Gemini only turns prepared RSS data into natural-language answers. The bot does not scrape Letterboxd profile or statistics pages.

## Commands

### Health and tracking

- `/ping` - replies exactly `Pong!`
- `/track username [channel] [all_ratings] [high_threshold] [low_threshold]` - creates or updates a subscription (Manage Server required)
- `/untrack username` - removes a subscription (Manage Server required)
- `/tracked` - lists tracked profiles, channels, and alert rules
- `/check-now` - immediately checks every tracked RSS feed (Manage Server required)

`/track` defaults to ratings **4+ or 1-**. Thresholds are inclusive and must use Letterboxd's half-star increments.

```text
/track username:alice
/track username:alice high_threshold:3.5 low_threshold:1.5
/track username:alice all_ratings:true
```

Changing `/track` options for an existing username updates that subscription without replaying old feed entries.

### RSS summaries and Gemini

- `/summary user username` - one tracked profile's stored RSS statistics, distribution, and timeline
- `/summary all` - combined RSS statistics and per-user averages for every profile tracked by the server
- `/ask username question` - asks Gemini about one tracked profile's observed RSS ratings

RSS results only represent entries the bot has observed. They are not guaranteed to be complete or all-time history.

### Letterboxd exports and all-time data

- `/import-letterboxd export_zip` - imports a Letterboxd export ZIP into the current Discord server
- `/summary all-time username` - summarizes one imported profile with an all-time chart
- `/remove-letterboxd-export username` - deletes an import (uploader or Manage Server required)

Download the ZIP from Letterboxd's data export feature and attach it directly to `/import-letterboxd`. Do not unzip it first. The importer:

- Reads `profile.csv` only to identify the Letterboxd username.
- Reads `ratings.csv` for the current rating, title, year, URI, and rating date of each rated film.
- Does **not** retain email, name, bio, location, comments, reviews, lists, likes, diary notes, tags, or the original ZIP.
- Validates file counts, paths, compressed/uncompressed sizes, required columns, ratings, and row limits without extracting files to disk.
- Scopes every import to the current Discord server and records its uploader.
- Allows replacement only by the original uploader or a member with Manage Server.

All server members can view `/summary all-time` after an export is imported. Here, “all-time” means the complete set of rated films present in the uploaded `ratings.csv`; unrated watched films are not part of its average.

### Comparisons

- `/compare rss usernames` - compares observed RSS data for tracked profiles
- `/compare all-time usernames` - compares imported export data

Discord does not support a truly variadic slash-command argument, so supply usernames in one comma- or space-separated option:

```text
/compare rss usernames:alice,bob movie-fan
/compare all-time usernames:alice bob charlie
```

Comparisons accept 2-50 unique usernames. They report rating count, average, highest-rated movie, and lowest-rated movie for every profile, paginate large comparisons, and attach a visual average-by-user chart.

## Visualizations and movie metadata

Summary and comparison PNGs are generated locally with Matplotlib:

- Single-user summaries show a rating distribution and rating history.
- Multi-user summaries/comparisons show a combined distribution and averages by profile.
- Charts display at most 20 profiles for legibility, while numeric calculations continue to use every selected profile and rating.

Summaries work without TMDB. Add a TMDB API Read Access Token to enrich the displayed highest/lowest films with director, cast, genres, runtime, and release year.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## 1. Create the Discord bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application.
2. Open **Bot**, create/reset the token, and keep it private.
3. Open **OAuth2 -> URL Generator**.
4. Select the `bot` and `applications.commands` scopes.
5. Grant **View Channels**, **Send Messages**, **Embed Links**, **Attach Files**, and **Read Message History**.
6. Open the generated URL to invite the bot.

No privileged intents or Message Content intent are needed.

## 2. Install and configure

Python 3.11 or newer is required. From this project directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:

```dotenv
DISCORD_TOKEN=your_real_bot_token
GEMINI_API_KEY=your_new_gemini_api_key
TMDB_READ_ACCESS_TOKEN=your_tmdb_api_read_access_token
```

Only `DISCORD_TOKEN` is required. RSS alerts, summaries, comparisons, export imports, and charts work without Gemini. Movie details work without TMDB but are less rich.

Create Gemini credentials in [Google AI Studio](https://aistudio.google.com/app/apikey). Create the TMDB read token from [TMDB API settings](https://developer.themoviedb.org/docs/getting-started). Never commit `.env`. Rotate any credential exposed in chat, logs, tickets, or commits.

For development, put a test server ID in `DEV_GUILD_ID` so command changes synchronize immediately. Leave it blank for global production commands.

## 3. Run

```powershell
python -m letterboxd_bot
```

The SQLite schema and existing 4+/1- subscriptions are migrated automatically at startup.

## Test

```powershell
python -m pip install -r requirements-dev.txt
pytest
```

Tests do not call Discord, Letterboxd, Gemini, or TMDB and require no real credentials.

## Deployment notes

- Keep the bot process running continuously.
- Persist `.env` and `data/` across deployments.
- Run only one process against a given SQLite file.
- Give the bot Attach Files permission so charts can be posted.
- Never commit tokens, export ZIPs, or the `data/` database.
- The RSS reader requests `https://letterboxd.com/{username}/rss/`.

## Project layout

```text
letterboxd_bot/
  bot.py            Discord commands, polling, embeds, and orchestration
  comparison.py     username-list parsing and per-user comparison results
  config.py         environment configuration and validation
  database.py       SQLite subscriptions, RSS entries, imports, and limits
  exports.py        bounded extraction-free Letterboxd ZIP/CSV parser
  gemini.py         grounded optional Gemini analysis
  metadata.py       optional TMDB movie metadata
  models.py         shared immutable data models
  rss.py            bounded Letterboxd RSS retrieval and parsing
  summary.py        deterministic rating aggregation
  visualization.py local PNG charts
tests/              offline parser, database, aggregation, and chart tests
```

## Important defaults and limits

```dotenv
HIGH_RATING_THRESHOLD=4.0
LOW_RATING_THRESHOLD=1.0
POLL_INTERVAL_SECONDS=300
MAX_TRACKED_PROFILES_PER_GUILD=100
GEMINI_MODEL=gemini-2.5-flash
GEMINI_COOLDOWN_SECONDS=20
GEMINI_DAILY_GUILD_LIMIT=100
GEMINI_MAX_ENTRIES=100
```

- Letterboxd export ZIP: 50 MB compressed, 250 MB declared uncompressed
- `ratings.csv`: 100,000 rows
- Comparison: 50 usernames
- Comparison chart: 20 displayed usernames
