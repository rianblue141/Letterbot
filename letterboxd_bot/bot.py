from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import defaultdict
from datetime import timezone
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Config
from .database import Database
from .gemini import GeminiError, GeminiService
from .metadata import MetadataError, MovieMetadata, TMDbClient
from .models import FeedEntry, RatingRecord, Subscription
from .rss import (
    FeedError,
    LetterboxdRSSClient,
    is_alert_rating,
    normalize_username,
    profile_url,
    rating_stars,
)
from .summary import RatingSummary, calculate_rating_summary
from .visualization import render_ratings_chart


LOGGER = logging.getLogger("letterboxd_bot")


class LetterboxdBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.database = Database(config.database_path)
        self.http_session: aiohttp.ClientSession | None = None
        self.gemini = (
            GeminiService(
                api_key=config.gemini_api_key,
                model=config.gemini_model,
                timeout_seconds=config.gemini_timeout_seconds,
            )
            if config.gemini_api_key
            else None
        )

    async def setup_hook(self) -> None:
        await self.database.initialize()
        timeout = aiohttp.ClientTimeout(total=self.config.http_timeout_seconds)
        self.http_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": "LetterboxdRatingAlerts/1.0 (Discord bot; RSS reader)"
            },
        )
        await self.add_cog(LetterboxdCog(self))

        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOGGER.info("Synced commands to development guild %s", guild.id)
        else:
            await self.tree.sync()
            LOGGER.info("Synced global application commands")

    async def close(self) -> None:
        if self.gemini is not None:
            try:
                await self.gemini.close()
            except Exception:
                LOGGER.exception("Could not close the Gemini client cleanly")
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")


class LetterboxdCog(commands.Cog):
    summary_group = app_commands.Group(
        name="summary", description="Summarize available ratings for tracked profiles."
    )

    def __init__(self, bot: LetterboxdBot) -> None:
        self.bot = bot
        if bot.http_session is None:
            raise RuntimeError("HTTP session was not initialized")
        self.rss = LetterboxdRSSClient(bot.http_session)
        self.tmdb = (
            TMDbClient(bot.http_session, bot.config.tmdb_read_access_token)
            if bot.config.tmdb_read_access_token
            else None
        )
        self._poll_lock = asyncio.Lock()
        self._ask_cooldowns: dict[int, float] = {}
        self.poll_feeds.change_interval(seconds=bot.config.poll_interval_seconds)
        self.poll_feeds.start()

    def cog_unload(self) -> None:
        self.poll_feeds.cancel()

    @app_commands.command(name="track", description="Track a Letterboxd profile's notable ratings.")
    @app_commands.describe(
        username="Letterboxd username (not the full profile URL)",
        channel="Where alerts should be posted; defaults to this channel",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def track(
        self,
        interaction: discord.Interaction,
        username: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        assert interaction.guild_id is not None

        try:
            username = normalize_username(username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        destination = channel or interaction.channel
        if not isinstance(destination, discord.TextChannel):
            await interaction.followup.send(
                "Choose a normal text channel for alerts.", ephemeral=True
            )
            return

        permissions = destination.permissions_for(interaction.guild.me)  # type: ignore[union-attr]
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            await interaction.followup.send(
                f"I need **View Channel**, **Send Messages**, and **Embed Links** in "
                f"{destination.mention}.",
                ephemeral=True,
            )
            return

        existing = await self.bot.database.get_subscription(
            interaction.guild_id, username
        )
        count = await self.bot.database.subscription_count(interaction.guild_id)
        if existing is None and count >= self.bot.config.max_tracked_profiles_per_guild:
            await interaction.followup.send(
                "This server has reached its tracked-profile limit.", ephemeral=True
            )
            return

        try:
            entries = await self.rss.fetch(username)
        except FeedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        async with self._poll_lock:
            created = await self.bot.database.upsert_subscription(
                guild_id=interaction.guild_id,
                username=username,
                channel_id=destination.id,
                created_by=interaction.user.id,
            )
            await self.bot.database.record_entries(username, entries)
            if created:
                await self.bot.database.seed_processed_entries(
                    interaction.guild_id, username, (entry.entry_id for entry in entries)
                )

        verb = "Now tracking" if created else "Updated"
        await interaction.followup.send(
            f"{verb} [{username}]({profile_url(username)}) in {destination.mention}. "
            f"I'll alert for ratings **{self.bot.config.high_rating_threshold:g}+** or "
            f"**{self.bot.config.low_rating_threshold:g}-**.",
            ephemeral=True,
        )

    @app_commands.command(name="untrack", description="Stop tracking a Letterboxd profile.")
    @app_commands.describe(username="Letterboxd username")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def untrack(self, interaction: discord.Interaction, username: str) -> None:
        assert interaction.guild_id is not None
        try:
            username = normalize_username(username)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        async with self._poll_lock:
            removed = await self.bot.database.remove_subscription(
                interaction.guild_id, username
            )
        message = (
            f"Stopped tracking `{username}`."
            if removed
            else f"`{username}` was not being tracked in this server."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="tracked", description="List profiles tracked in this server.")
    @app_commands.guild_only()
    async def tracked(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        subscriptions = await self.bot.database.list_subscriptions(interaction.guild_id)
        if not subscriptions:
            await interaction.response.send_message(
                "No Letterboxd profiles are tracked here yet.", ephemeral=True
            )
            return
        lines = [
            f"• [`{item.username}`]({profile_url(item.username)}) → <#{item.channel_id}>"
            for item in subscriptions
        ]
        await interaction.response.send_message(
            "**Tracked Letterboxd profiles**\n" + "\n".join(lines), ephemeral=True
        )

    @app_commands.command(name="check-now", description="Check all tracked RSS feeds now.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def check_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        assert interaction.guild_id is not None
        subscriptions = await self.bot.database.list_subscriptions(interaction.guild_id)
        if not subscriptions:
            await interaction.followup.send("There are no profiles to check.", ephemeral=True)
            return
        await self._poll_subscriptions(subscriptions)
        await interaction.followup.send("RSS check complete.", ephemeral=True)

    @summary_group.command(
        name="user", description="Summarize available ratings for one tracked user."
    )
    @app_commands.describe(username="A Letterboxd username tracked in this server")
    @app_commands.guild_only()
    async def summary_user(
        self, interaction: discord.Interaction, username: str
    ) -> None:
        await interaction.response.defer()
        assert interaction.guild_id is not None
        try:
            username = normalize_username(username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        subscription = await self.bot.database.get_subscription(
            interaction.guild_id, username
        )
        if subscription is None:
            await interaction.followup.send(
                f"`{username}` is not tracked in this server.", ephemeral=True
            )
            return
        await self._send_rating_summary(
            interaction,
            usernames=[username],
            title=f"Letterboxd summary: {username}",
            include_rater=False,
        )

    @summary_group.command(
        name="all", description="Combine available ratings for all tracked users."
    )
    @app_commands.guild_only()
    async def summary_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        assert interaction.guild_id is not None
        subscriptions = await self.bot.database.list_subscriptions(interaction.guild_id)
        if not subscriptions:
            await interaction.followup.send(
                "No Letterboxd profiles are tracked in this server.", ephemeral=True
            )
            return
        usernames = [subscription.username for subscription in subscriptions]
        await self._send_rating_summary(
            interaction,
            usernames=usernames,
            title=f"Combined Letterboxd summary ({len(usernames)} users)",
            include_rater=True,
        )

    @app_commands.command(
        name="ask", description="Ask Gemini about a tracked profile's observed ratings."
    )
    @app_commands.describe(
        username="A Letterboxd username tracked in this server",
        question="A question about ratings the bot has observed",
    )
    @app_commands.guild_only()
    async def ask(
        self, interaction: discord.Interaction, username: str, question: str
    ) -> None:
        await interaction.response.defer()
        assert interaction.guild_id is not None

        if self.bot.gemini is None:
            await interaction.followup.send(
                "Gemini is not configured. Add `GEMINI_API_KEY` to the bot's `.env` file.",
                ephemeral=True,
            )
            return

        try:
            username = normalize_username(username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        question = question.strip()
        if not question:
            await interaction.followup.send("Please enter a question.", ephemeral=True)
            return
        if len(question) > self.bot.config.gemini_question_max_chars:
            await interaction.followup.send(
                f"Keep the question under "
                f"{self.bot.config.gemini_question_max_chars} characters.",
                ephemeral=True,
            )
            return

        subscription = await self.bot.database.get_subscription(
            interaction.guild_id, username
        )
        if subscription is None:
            await interaction.followup.send(
                f"`{username}` is not tracked in this server.", ephemeral=True
            )
            return

        now = time.monotonic()
        last_request = self._ask_cooldowns.get(interaction.user.id, 0.0)
        retry_after = self.bot.config.gemini_cooldown_seconds - (now - last_request)
        if retry_after > 0:
            await interaction.followup.send(
                f"Please wait {int(retry_after) + 1} seconds before asking again.",
                ephemeral=True,
            )
            return

        profile_data = await self.bot.database.get_profile_context(
            username,
            limit=self.bot.config.gemini_max_entries,
            high_threshold=self.bot.config.high_rating_threshold,
            low_threshold=self.bot.config.low_rating_threshold,
        )
        if profile_data is None:
            await interaction.followup.send(
                f"I do not have any rated RSS entries for `{username}` yet.",
                ephemeral=True,
            )
            return

        within_quota = await self.bot.database.consume_gemini_quota(
            interaction.guild_id, self.bot.config.gemini_daily_guild_limit
        )
        if not within_quota:
            await interaction.followup.send(
                "This server has reached its daily Gemini request limit.",
                ephemeral=True,
            )
            return

        self._ask_cooldowns[interaction.user.id] = now
        try:
            answer = await self.bot.gemini.answer_profile_question(
                username=username,
                question=question,
                profile_data=profile_data,
            )
        except GeminiError as exc:
            LOGGER.warning("Gemini request failed for %s: %s", username, exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        observed_count = profile_data["total_observed_ratings"]
        await interaction.followup.send(
            f"**Gemini analysis for `{username}`**\n{answer}\n"
            f"-# Based on {observed_count} rating(s) observed through RSS; not all-time data.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @track.error
    @untrack.error
    @check_now.error
    @ask.error
    @summary_user.error
    @summary_all.error
    async def management_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Server** permission to use this command."
        else:
            LOGGER.exception("Application command failed", exc_info=error)
            message = "That command failed unexpectedly. Check the bot logs for details."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _send_rating_summary(
        self,
        interaction: discord.Interaction,
        *,
        usernames: list[str],
        title: str,
        include_rater: bool,
    ) -> None:
        records = await self.bot.database.get_rating_records(usernames)
        summary = calculate_rating_summary(records)
        if summary is None:
            await interaction.followup.send(
                "No rated RSS entries are available for that selection yet.",
                ephemeral=True,
            )
            return

        top_metadata: MovieMetadata | None = None
        lowest_metadata: MovieMetadata | None = None
        metadata_note: str | None = None
        if self.tmdb is None:
            metadata_note = (
                "Add `TMDB_READ_ACCESS_TOKEN` to `.env` for director, cast, genre, "
                "runtime, and release details."
            )
        else:
            try:
                if _same_movie(summary.top_rated, summary.lowest_rated):
                    top_metadata = await self.tmdb.get_movie(summary.top_rated)
                    lowest_metadata = top_metadata
                else:
                    top_metadata, lowest_metadata = await asyncio.gather(
                        self.tmdb.get_movie(summary.top_rated),
                        self.tmdb.get_movie(summary.lowest_rated),
                    )
                if top_metadata is None or lowest_metadata is None:
                    metadata_note = "Some movie metadata could not be matched on TMDB."
            except MetadataError as exc:
                LOGGER.warning("Summary metadata lookup failed: %s", exc)
                metadata_note = str(exc)

        embed = build_summary_embed(
            title=title,
            summary=summary,
            top_metadata=top_metadata,
            lowest_metadata=lowest_metadata,
            include_rater=include_rater,
            metadata_note=metadata_note,
        )
        chart_file: discord.File | None = None
        try:
            chart_bytes = await asyncio.to_thread(
                render_ratings_chart,
                records,
                summary,
                title=title,
                combined=include_rater,
            )
            chart_filename = "letterboxd-ratings.png"
            chart_file = discord.File(io.BytesIO(chart_bytes), filename=chart_filename)
            embed.set_image(url=f"attachment://{chart_filename}")
        except Exception:
            LOGGER.exception("Could not render ratings visualization")

        if chart_file is not None:
            await interaction.followup.send(
                embed=embed,
                file=chart_file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )

    @tasks.loop(seconds=300)
    async def poll_feeds(self) -> None:
        try:
            subscriptions = await self.bot.database.list_subscriptions()
            if subscriptions:
                await self._poll_subscriptions(subscriptions)
        except Exception:
            # A transient database, network, or Discord failure should not kill
            # the scheduler. The next interval will retry unprocessed entries.
            LOGGER.exception("RSS polling interval failed")

    @poll_feeds.before_loop
    async def before_poll_feeds(self) -> None:
        await self.bot.wait_until_ready()

    @poll_feeds.error
    async def poll_feeds_error(self, error: BaseException) -> None:
        LOGGER.exception("RSS polling loop failed", exc_info=error)

    async def _poll_subscriptions(self, subscriptions: list[Subscription]) -> None:
        async with self._poll_lock:
            by_username: dict[str, list[Subscription]] = defaultdict(list)
            for subscription in subscriptions:
                by_username[subscription.username].append(subscription)

            semaphore = asyncio.Semaphore(5)

            async def process(username: str, items: list[Subscription]) -> None:
                async with semaphore:
                    try:
                        entries = await self.rss.fetch(username)
                    except FeedError as exc:
                        LOGGER.warning("%s", exc)
                        return
                    await self._process_entries(username, items, entries)

            await asyncio.gather(
                *(process(username, items) for username, items in by_username.items())
            )

    async def _process_entries(
        self,
        username: str,
        subscriptions: list[Subscription],
        entries: list[FeedEntry],
    ) -> None:
        await self.bot.database.record_entries(username, entries)
        # Letterboxd feeds are normally newest-first. Reverse them so multiple new
        # ratings are announced in the order they happened.
        for entry in reversed(entries):
            for subscription in subscriptions:
                if await self.bot.database.is_processed(
                    subscription.guild_id, username, entry.entry_id
                ):
                    continue

                should_alert = is_alert_rating(
                    entry.rating,
                    high_threshold=self.bot.config.high_rating_threshold,
                    low_threshold=self.bot.config.low_rating_threshold,
                )
                if should_alert:
                    sent = await self._send_alert(subscription, username, entry)
                    if not sent:
                        continue

                await self.bot.database.mark_processed(
                    subscription.guild_id, username, entry.entry_id
                )

    async def _send_alert(
        self, subscription: Subscription, username: str, entry: FeedEntry
    ) -> bool:
        channel = self.bot.get_channel(subscription.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(subscription.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Cannot access channel %s for guild %s",
                    subscription.channel_id,
                    subscription.guild_id,
                )
                return False
        if not isinstance(channel, discord.TextChannel):
            return False

        try:
            await channel.send(
                embed=build_rating_embed(
                    username,
                    entry,
                    is_high=entry.rating >= self.bot.config.high_rating_threshold,
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception(
                "Could not send alert for %s to channel %s",
                username,
                subscription.channel_id,
            )
            return False
        return True


def build_rating_embed(
    username: str, entry: FeedEntry, *, is_high: bool
) -> discord.Embed:
    assert entry.rating is not None
    color = (
        discord.Color.from_rgb(0, 224, 84)
        if is_high
        else discord.Color.from_rgb(255, 128, 0)
    )
    year = f" ({entry.film_year})" if entry.film_year else ""
    safe_link = entry.link if _is_safe_letterboxd_url(entry.link) else profile_url(username)
    embed = discord.Embed(
        title=f"{entry.film_title}{year}"[:256],
        url=safe_link,
        description=(
            f"[{discord.utils.escape_markdown(username)}]({profile_url(username)}) rated it "
            f"**{entry.rating:g}/5**  {rating_stars(entry.rating)}"
        ),
        color=color,
    )
    if entry.watched_date:
        embed.add_field(
            name="Watched",
            value=(
                f"{entry.watched_date.strftime('%B')} "
                f"{entry.watched_date.day}, {entry.watched_date.year}"
            ),
            inline=True,
        )
    if entry.published_at:
        embed.timestamp = entry.published_at.astimezone(timezone.utc)
    embed.set_footer(text="Via Letterboxd RSS")
    return embed


def build_summary_embed(
    *,
    title: str,
    summary: RatingSummary,
    top_metadata: MovieMetadata | None,
    lowest_metadata: MovieMetadata | None,
    include_rater: bool,
    metadata_note: str | None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title[:256],
        color=discord.Color.from_rgb(64, 188, 244),
        description="Statistics use every rated RSS entry currently stored by the bot.",
    )
    embed.add_field(
        name="Available ratings", value=str(summary.total_ratings), inline=True
    )
    embed.add_field(
        name="Average rating",
        value=f"{summary.average_rating:.2f}/5",
        inline=True,
    )
    embed.add_field(
        name="Top rated movie",
        value=_format_summary_movie(
            summary.top_rated, top_metadata, include_rater=include_rater
        ),
        inline=False,
    )
    embed.add_field(
        name="Lowest rated movie",
        value=_format_summary_movie(
            summary.lowest_rated, lowest_metadata, include_rater=include_rater
        ),
        inline=False,
    )
    if include_rater:
        lines = [
            f"`{discord.utils.escape_markdown(username)}`: {average:.2f}/5"
            for username, average in summary.per_user_averages.items()
        ]
        embed.add_field(
            name="Average by tracked user",
            value=_fit_discord_field(lines),
            inline=False,
        )
    if metadata_note:
        embed.add_field(
            name="Movie metadata",
            value=metadata_note[:1024],
            inline=False,
        )
    footer = "Based on stored Letterboxd RSS data; not guaranteed all-time."
    if top_metadata is not None or lowest_metadata is not None:
        footer += " Movie metadata: TMDB."
    embed.set_footer(text=footer)
    return embed


def _format_summary_movie(
    record: RatingRecord,
    metadata: MovieMetadata | None,
    *,
    include_rater: bool,
) -> str:
    safe_title = discord.utils.escape_markdown(record.film_title)
    year = metadata.release_year if metadata and metadata.release_year else record.film_year
    year_suffix = f" ({year})" if year else ""
    link = record.link if _is_safe_letterboxd_url(record.link) else profile_url(record.username)
    lines = [
        f"**[{safe_title}{year_suffix}]({link})** — "
        f"**{record.rating:g}/5** {rating_stars(record.rating)}"
    ]
    if include_rater:
        lines.append(f"Rated by `{discord.utils.escape_markdown(record.username)}`")
    if metadata is not None:
        if year:
            lines.append(f"Release year: {year}")
        if metadata.directors:
            lines.append(
                "Director: "
                + ", ".join(
                    discord.utils.escape_markdown(name) for name in metadata.directors
                )
            )
        if metadata.actors:
            lines.append(
                "Cast: "
                + ", ".join(
                    discord.utils.escape_markdown(name) for name in metadata.actors
                )
            )
        if metadata.genres:
            lines.append(
                "Genres: "
                + ", ".join(
                    discord.utils.escape_markdown(name) for name in metadata.genres
                )
            )
        if metadata.runtime_minutes:
            lines.append(f"Runtime: {metadata.runtime_minutes} minutes")
    else:
        lines.append("Additional movie details unavailable.")
    value = "\n".join(lines)
    return value if len(value) <= 1024 else value[:1021] + "..."


def _fit_discord_field(lines: list[str]) -> str:
    selected: list[str] = []
    length = 0
    for line in lines:
        extra = len(line) + (1 if selected else 0)
        if length + extra > 1000:
            break
        selected.append(line)
        length += extra
    omitted = len(lines) - len(selected)
    if omitted:
        selected.append(f"...and {omitted} more")
    return "\n".join(selected) or "No per-user data available."


def _same_movie(first: RatingRecord, second: RatingRecord) -> bool:
    if first.tmdb_id is not None and second.tmdb_id is not None:
        return first.tmdb_id == second.tmdb_id
    return (
        first.film_title.casefold() == second.film_title.casefold()
        and first.film_year == second.film_year
    )


def _is_safe_letterboxd_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and (
        parsed.hostname == "letterboxd.com" or parsed.hostname == "www.letterboxd.com"
    )


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    LetterboxdBot(config).run(config.discord_token, log_handler=None)
