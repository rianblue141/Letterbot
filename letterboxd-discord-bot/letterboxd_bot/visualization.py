from __future__ import annotations

import io
import threading
from collections import Counter
from datetime import datetime

import matplotlib

matplotlib.use("Agg")

from matplotlib import dates as mdates  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402

from .models import RatingRecord  # noqa: E402
from .summary import RatingSummary  # noqa: E402


BACKGROUND = "#14181c"
PANEL = "#202830"
TEXT = "#f4f4f4"
MUTED = "#9ab0c0"
GREEN = "#00e054"
ORANGE = "#ff8000"
BLUE = "#40bcf4"
GRID = "#3b4650"
MAX_USERS_IN_CHART = 20

# Matplotlib's global state is not thread-safe. Summary rendering is offloaded
# from Discord's event loop, so serialize renders across simultaneous commands.
_RENDER_LOCK = threading.Lock()


def render_ratings_chart(
    records: list[RatingRecord],
    summary: RatingSummary,
    *,
    title: str,
    combined: bool,
) -> bytes:
    with _RENDER_LOCK:
        figure, axes = plt.subplots(1, 2, figsize=(11, 5.5), dpi=130)
        try:
            figure.patch.set_facecolor(BACKGROUND)
            for axis in axes:
                _style_axis(axis)

            _draw_distribution(axes[0], records, summary.average_rating)
            if combined:
                _draw_user_averages(axes[1], summary)
            else:
                _draw_rating_history(axes[1], records)

            figure.suptitle(title, color=TEXT, fontsize=16, fontweight="bold", y=0.98)
            figure.tight_layout(rect=(0, 0, 1, 0.94))
            output = io.BytesIO()
            figure.savefig(
                output,
                format="png",
                facecolor=figure.get_facecolor(),
                bbox_inches="tight",
            )
            return output.getvalue()
        finally:
            plt.close(figure)


def _draw_distribution(axis: object, records: list[RatingRecord], average: float) -> None:
    ratings = [step / 2 for step in range(1, 11)]
    counts = Counter(record.rating for record in records)
    values = [counts.get(rating, 0) for rating in ratings]
    colors = [_rating_color(rating) for rating in ratings]
    bars = axis.bar(range(len(ratings)), values, color=colors, width=0.72)
    axis.set_title("Rating distribution", color=TEXT, fontsize=12, fontweight="bold")
    axis.set_xlabel("Rating", color=MUTED)
    axis.set_ylabel("Entries", color=MUTED)
    axis.set_xticks(range(len(ratings)), [f"{rating:g}" for rating in ratings])
    axis.yaxis.get_major_locator().set_params(integer=True)
    axis.grid(axis="y", color=GRID, alpha=0.55, linewidth=0.8)
    axis.set_axisbelow(True)
    for bar, count in zip(bars, values, strict=True):
        if count:
            axis.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height(),
                str(count),
                ha="center",
                va="bottom",
                color=TEXT,
                fontsize=9,
            )
    axis.text(
        0.98,
        0.95,
        f"Average: {average:.2f}",
        transform=axis.transAxes,
        ha="right",
        va="top",
        color=TEXT,
        bbox={"facecolor": BACKGROUND, "edgecolor": GRID, "boxstyle": "round,pad=0.4"},
    )


def _draw_user_averages(axis: object, summary: RatingSummary) -> None:
    all_users = sorted(
        summary.per_user_averages.items(), key=lambda item: item[1], reverse=True
    )
    users = all_users[:MAX_USERS_IN_CHART]
    users.reverse()
    labels = [_short_label(username) for username, _ in users]
    averages = [average for _, average in users]
    colors = [_rating_color(average) for average in averages]
    bars = axis.barh(labels, averages, color=colors, height=0.68)
    suffix = (
        f" (top {MAX_USERS_IN_CHART})"
        if len(all_users) > MAX_USERS_IN_CHART
        else ""
    )
    axis.set_title(
        f"Average by tracked user{suffix}",
        color=TEXT,
        fontsize=12,
        fontweight="bold",
    )
    axis.set_xlabel("Average rating", color=MUTED)
    axis.set_xlim(0, 5.15)
    axis.set_xticks([0, 1, 2, 3, 4, 5])
    axis.grid(axis="x", color=GRID, alpha=0.55, linewidth=0.8)
    axis.set_axisbelow(True)
    for bar, average in zip(bars, averages, strict=True):
        axis.text(
            min(average + 0.06, 5.03),
            bar.get_y() + bar.get_height() / 2,
            f"{average:.2f}",
            va="center",
            ha="left",
            color=TEXT,
            fontsize=9,
        )


def _draw_rating_history(axis: object, records: list[RatingRecord]) -> None:
    dated = [
        (date_value, record.rating)
        for record in records
        if (date_value := _record_date(record)) is not None
    ]
    dated.sort(key=lambda item: item[0])
    axis.set_title("Observed rating history", color=TEXT, fontsize=12, fontweight="bold")
    axis.set_ylabel("Rating", color=MUTED)
    axis.set_ylim(0.25, 5.25)
    axis.set_yticks([0.5, 1, 2, 3, 4, 5])
    axis.grid(color=GRID, alpha=0.55, linewidth=0.8)
    axis.set_axisbelow(True)

    if dated:
        dates = [item[0] for item in dated]
        ratings = [item[1] for item in dated]
        axis.plot(dates, ratings, color=BLUE, alpha=0.55, linewidth=1.5)
        axis.scatter(
            dates,
            ratings,
            c=[_rating_color(rating) for rating in ratings],
            s=48,
            edgecolors=TEXT,
            linewidths=0.5,
            zorder=3,
        )
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        axis.tick_params(axis="x", rotation=35)
        axis.set_xlabel("Watched / published date", color=MUTED)
    else:
        ordered = list(reversed(records))
        positions = list(range(1, len(ordered) + 1))
        ratings = [record.rating for record in ordered]
        axis.plot(positions, ratings, color=BLUE, alpha=0.55, linewidth=1.5)
        axis.scatter(
            positions,
            ratings,
            c=[_rating_color(rating) for rating in ratings],
            s=48,
            edgecolors=TEXT,
            linewidths=0.5,
            zorder=3,
        )
        axis.set_xlabel("Observed entry order", color=MUTED)


def _style_axis(axis: object) -> None:
    axis.set_facecolor(PANEL)
    axis.tick_params(colors=MUTED)
    for spine in axis.spines.values():
        spine.set_color(GRID)


def _record_date(record: RatingRecord) -> datetime | None:
    value = record.watched_date or record.published_at
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _rating_color(rating: float) -> str:
    if rating >= 4.0:
        return GREEN
    if rating <= 1.0:
        return ORANGE
    return BLUE


def _short_label(value: str, maximum: int = 22) -> str:
    return value if len(value) <= maximum else value[: maximum - 1] + "…"
