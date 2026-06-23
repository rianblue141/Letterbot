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
from .comparison import (
    UserComparison,
    calculate_user_comparisons,
    parse_username_list,
)
from .database import Database
from .exports import (
    MAX_EXPORT_ZIP_BYTES,
    ExportError,
    parse_letterboxd_export,
)
from .gemini import GeminiError, GeminiService
from .metadata import MetadataError, MovieMetadata, TMDbClient
from .models import FeedEntry, RatingRecord, Subscription
from .rss import (
    FeedError,
    LetterboxdRSSClient,
    is_subscription_alert_rating,
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
        name="summary", description="Summarize stored ratings for Letterboxd profiles."
    )
    compare_group = app_commands.Group(
        name="compare", description="Compare ratings for multiple Letterboxd users."
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
        all_ratings="Alert for every rating and ignore the threshold options",
        high_threshold="Alert at or above this rating (default: 4)",
        low_threshold="Alert at or below this rating (default: 1)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def track(
        self,
        interaction: discord.Interaction,
        username: str,
        channel: discord.TextChannel | None = None,
        all_ratings: bool = False,
        high_threshold: app_commands.Range[float, 0.5, 5.0] = 4.0,
        low_threshold: app_commands.Range[float, 0.5, 5.0] = 1.0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        assert interaction.guild_id is not None

        try:
            username = normalize_username(username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if not _is_half_star_value(high_threshold) or not _is_half_star_value(
            low_threshold
        ):
            await interaction.followup.send(
                "Thresholds must use Letterboxd's half-star steps, such as `3.5`.",
                ephemeral=True,
            )
            return
        if not all_ratings and low_threshold >= high_threshold:
            await interaction.followup.send(
                "The low threshold must be below the high threshold.", ephemeral=True
            )
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
                track_all=all_ratings,
                high_threshold=high_threshold,
                low_threshold=low_threshold,
            )
            await self.bot.database.record_entries(username, entries)
            if created:
                await self.bot.database.seed_processed_entries(
                    interaction.guild_id, username, (entry.entry_id for entry in entries)
                )

        verb = "Now tracking" if created else "Updated"
        rule = (
            "every rating"
            if all_ratings
            else f"ratings **{high_threshold:g}+** or **{low_threshold:g}-**"
        )
        await interaction.followup.send(
            f"{verb} [{username}]({profile_url(username)}) in {destination.mention}. "
            f"I'll alert for {rule}.",
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
        lines = []
        for item in subscriptions:
            rule = (
                "all ratings"
                if item.track_all
                else f"{item.high_threshold:g}+ / {item.low_threshold:g}-"
            )
            lines.append(
                f"• [`{item.username}`]({profile_url(item.username)}) → "
                f"<#{item.channel_id}> ({rule})"
            )
        await interaction.response.send_message(
            "**Tracked Letterboxd profiles**\n" + "\n".join(lines), ephemeral=True
        )

    @app_commands.command(name="ping", description="Check whether the bot is online.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Pong!")

    @app_commands.command(
        name="import-letterboxd",
        description="Import your Letterboxd export ZIP for all-time summaries.",
    )
    @app_commands.describe(export_zip="The ZIP downloaded from Letterboxd's export page")
    @app_commands.guild_only()
    async def import_letterboxd(
        self, interaction: discord.Interaction, export_zip: discord.Attachment
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        assert interaction.guild_id is not None
        if not export_zip.filename.casefold().endswith(".zip"):
            await interaction.followup.send(
                "Upload the `.zip` file produced by Letterboxd.", ephemeral=True
            )
            return
        if export_zip.size > MAX_EXPORT_ZIP_BYTES:
            await interaction.followup.send(
                "That Letterboxd export ZIP is too large.", ephemeral=True
            )
            return
        try:
            data = await export_zip.read()
            parsed = await asyncio.to_thread(parse_letterboxd_export, data)
        except ExportError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "Discord could not download that attachment. Please try again.",
                ephemeral=True,
            )
            return

        existing_owner = await self.bot.database.get_all_time_import_owner(
            interaction.guild_id, parsed.username
        )
        can_manage = bool(
            getattr(interaction.user, "guild_permissions", None)
            and interaction.user.guild_permissions.manage_guild  # type: ignore[union-attr]
        )
        if (
            existing_owner is not None
            and existing_owner != interaction.user.id
            and not can_manage
        ):
            await interaction.followup.send(
                "Another member imported that profile. Only its uploader or a server "
                "manager can replace it.",
                ephemeral=True,
            )
            return

        count = await self.bot.database.replace_all_time_ratings(
            guild_id=interaction.guild_id,
            username=parsed.username,
            uploaded_by=interaction.user.id,
            source_filename=export_zip.filename,
            records=parsed.ratings,
        )
        await interaction.followup.send(
            f"Imported **{count:,}** all-time ratings for `{parsed.username}`. "
            f"Use `/summary all-time` to view them.",
            ephemeral=True,
        )

    @app_commands.command(
        name="remove-letterboxd-export",
        description="Remove an imported Letterboxd all-time dataset.",
    )
    @app_commands.describe(username="Letterboxd username whose import should be removed")
    @app_commands.guild_only()
    async def remove_letterboxd_export(
        self, interaction: discord.Interaction, username: str
    ) -> None:
        assert interaction.guild_id is not None
        try:
            username = normalize_username(username)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        owner = await self.bot.database.get_all_time_import_owner(
            interaction.guild_id, username
        )
        can_manage = bool(
            getattr(interaction.user, "guild_permissions", None)
            and interaction.user.guild_permissions.manage_guild  # type: ignore[union-attr]
        )
        if owner is None:
            await interaction.response.send_message(
                f"No all-time export is stored for `{username}`.", ephemeral=True
            )
            return
        if owner != interaction.user.id and not can_manage:
            await interaction.response.send_message(
                "Only the uploader or a server manager can remove that import.",
                ephemeral=True,
            )
            return
        await self.bot.database.delete_all_time_import(interaction.guild_id, username)
        await interaction.response.send_message(
            f"Removed the all-time export for `{username}`.", ephemeral=True
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

    @summary_group.command(
        name="all-time",
        description="Summarize one user's imported Letterboxd export.",
    )
    @app_commands.describe(username="A username with an imported Letterboxd export")
    @app_commands.guild_only()
    async def summary_all_time(
        self, interaction: discord.Interaction, username: str
    ) -> None:
        await interaction.response.defer()
        assert interaction.guild_id is not None
        try:
            username = normalize_username(username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        records = await self.bot.database.get_all_time_rating_records(
            interaction.guild_id, [username]
        )
        if not records:
            await interaction.followup.send(
                f"No Letterboxd export is available for `{username}`. Upload it with "
                f"`/import-letterboxd` first.",
                ephemeral=True,
            )
            return
        await self._send_rating_summary(
            interaction,
            usernames=[username],
            title=f"All-time Letterboxd summary: {username}",
            include_rater=False,
            records=records,
            data_scope="User-provided Letterboxd ratings export",
        )

    @compare_group.command(
        name="rss", description="Compare stored RSS ratings for multiple tracked users."
    )
    @app_commands.describe(
        usernames="Two or more tracked usernames separated by spaces or commas"
    )
    @app_commands.guild_only()
    async def compare_rss(
        self, interaction: discord.Interaction, usernames: str
    ) -> None:
        await interaction.response.defer()
        assert interaction.guild_id is not None
        try:
            requested = parse_username_list(usernames)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        tracked = {
            subscription.username
            for subscription in await self.bot.database.list_subscriptions(
                interaction.guild_id
            )
        }
        missing = [username for username in requested if username not in tracked]
        if missing:
            await interaction.followup.send(
                "These users are not tracked in this server: "
                + ", ".join(f"`{username}`" for username in missing),
                ephemeral=True,
            )
            return
        records = await self.bot.database.get_rating_records(requested)
        await self._send_comparison(
            interaction,
            usernames=requested,
            records=records,
            source_label="Stored Letterboxd RSS ratings",
        )

    @compare_group.command(
        name="all-time",
        description="Compare imported all-time ratings for multiple users.",
    )
    @app_commands.describe(
        usernames="Two or more imported usernames separated by spaces or commas"
    )
    @app_commands.guild_only()
    async def compare_all_time(
        self, interaction: discord.Interaction, usernames: str
    ) -> None:
        await interaction.response.defer()
        assert interaction.guild_id is not None
        try:
            requested = parse_username_list(usernames)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        imported = set(
            await self.bot.database.list_all_time_usernames(interaction.guild_id)
        )
        missing = [username for username in requested if username not in imported]
        if missing:
            await interaction.followup.send(
                "These users need a Letterboxd export imported first: "
                + ", ".join(f"`{username}`" for username in missing),
                ephemeral=True,
            )
            return
        records = await self.bot.database.get_all_time_rating_records(
            interaction.guild_id, requested
        )
        await self._send_comparison(
            interaction,
            usernames=requested,
            records=records,
            source_label="User-provided Letterboxd exports",
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
    @summary_all_time.error
    @import_letterboxd.error
    @remove_letterboxd_export.error
    @compare_rss.error
    @compare_all_time.error
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
        records: list[RatingRecord] | None = None,
        data_scope: str = "Stored Letterboxd RSS entries",
    ) -> None:
        if records is None:
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
            data_scope=data_scope,
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

    async def _send_comparison(
        self,
        interaction: discord.Interaction,
        *,
        usernames: list[str],
        records: list[RatingRecord],
        source_label: str,
    ) -> None:
        comparisons = calculate_user_comparisons(records, usernames)
        users_with_data = {comparison.username for comparison in comparisons}
        missing_data = [
            username for username in usernames if username not in users_with_data
        ]
        if missing_data:
            await interaction.followup.send(
                "No rating data is available for: "
                + ", ".join(f"`{username}`" for username in missing_data),
                ephemeral=True,
            )
            return
        combined_summary = calculate_rating_summary(records)
        if combined_summary is None:
            await interaction.followup.send(
                "No ratings are available for that comparison.", ephemeral=True
            )
            return

        embeds = build_comparison_embeds(
            comparisons=comparisons,
            combined_summary=combined_summary,
            source_label=source_label,
        )
        chart_file: discord.File | None = None
        try:
            chart_bytes = await asyncio.to_thread(
                render_ratings_chart,
                records,
                combined_summary,
                title=f"Letterboxd comparison ({len(usernames)} users)",
                combined=True,
            )
            chart_filename = "letterboxd-comparison.png"
            chart_file = discord.File(io.BytesIO(chart_bytes), filename=chart_filename)
            embeds[0].set_image(url=f"attachment://{chart_filename}")
        except Exception:
            LOGGER.exception("Could not render comparison visualization")

        if chart_file is not None:
            await interaction.followup.send(
                embeds=embeds,
                file=chart_file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                embeds=embeds, allowed_mentions=discord.AllowedMentions.none()
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

                should_alert = is_subscription_alert_rating(
                    entry.rating,
                    track_all=subscription.track_all,
                    high_threshold=subscription.high_threshold,
                    low_threshold=subscription.low_threshold,
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
                    alert_type=(
                        "high"
                        if entry.rating >= subscription.high_threshold
                        else "low"
                        if entry.rating <= subscription.low_threshold
                        else "all"
                    ),
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
    username: str, entry: FeedEntry, *, alert_type: str
) -> discord.Embed:
    assert entry.rating is not None
    color = (
        discord.Color.from_rgb(0, 224, 84)
        if alert_type == "high"
        else discord.Color.from_rgb(255, 128, 0)
        if alert_type == "low"
        else discord.Color.from_rgb(64, 188, 244)
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
    data_scope: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=title[:256],
        color=discord.Color.from_rgb(64, 188, 244),
        description=f"Statistics use every rating in: **{data_scope}**.",
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
    footer = (
        "Based on a user-provided Letterboxd export."
        if "export" in data_scope.casefold()
        else "Based on stored Letterboxd RSS data; not guaranteed all-time."
    )
    if top_metadata is not None or lowest_metadata is not None:
        footer += " Movie metadata: TMDB."
    embed.set_footer(text=footer)
    return embed


def build_comparison_embeds(
    *,
    comparisons: list[UserComparison],
    combined_summary: RatingSummary,
    source_label: str,
) -> list[discord.Embed]:
    page_size = 10
    page_count = (len(comparisons) + page_size - 1) // page_size
    embeds: list[discord.Embed] = []
    for page_index in range(page_count):
        page = comparisons[page_index * page_size : (page_index + 1) * page_size]
        title = "Letterboxd user comparison"
        if page_count > 1:
            title += f" ({page_index + 1}/{page_count})"
        description = (
            f"Source: **{source_label}**\n"
            f"Combined: **{combined_summary.total_ratings:,} ratings**, "
            f"**{combined_summary.average_rating:.2f}/5 average**"
            if page_index == 0
            else f"Source: **{source_label}**"
        )
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.from_rgb(64, 188, 244),
        )
        for comparison in page:
            summary = comparison.summary
            top_year = (
                f" ({summary.top_rated.film_year})"
                if summary.top_rated.film_year
                else ""
            )
            low_year = (
                f" ({summary.lowest_rated.film_year})"
                if summary.lowest_rated.film_year
                else ""
            )
            top_title = discord.utils.escape_markdown(
                summary.top_rated.film_title
            )
            low_title = discord.utils.escape_markdown(
                summary.lowest_rated.film_title
            )
            value = (
                f"**{summary.total_ratings:,}** ratings · "
                f"**{summary.average_rating:.2f}/5** average\n"
                f"Highest: {top_title}{top_year} — {summary.top_rated.rating:g}/5\n"
                f"Lowest: {low_title}{low_year} — {summary.lowest_rated.rating:g}/5"
            )
            if len(value) > 1024:
                value = value[:1021] + "..."
            embed.add_field(
                name=discord.utils.escape_markdown(comparison.username)[:256],
                value=value,
                inline=False,
            )
        footer = "All-time data comes from user-provided Letterboxd exports."
        if "RSS" in source_label:
            footer = "RSS comparisons use only entries observed by the bot."
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


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


def _is_half_star_value(value: float) -> bool:
    return abs(value * 2 - round(value * 2)) < 1e-9


def _is_safe_letterboxd_url(value: str) -> bool:
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
