#!/usr/bin/env python3
"""Generate the profile's local GitHub contribution graphic."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
DAYS_IN_WINDOW = 365
DEFAULT_OUTPUT = Path("assets/generated/contributions.svg")
MONTH_LABELS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

CONTRIBUTIONS_QUERY = """
query ProfileContributions($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks {
          contributionDays {
            contributionCount
            date
          }
        }
      }
    }
  }
}
""".strip()


class ProfileStatsError(RuntimeError):
    """Raised when contribution data cannot be retrieved or validated."""


@dataclass(frozen=True, order=True)
class ContributionDay:
    """One UTC calendar date and its GitHub contribution count."""

    day: date
    count: int

    def __post_init__(self) -> None:
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise TypeError("contribution counts must be integers")
        if self.count < 0:
            raise ValueError("contribution counts cannot be negative")


@dataclass(frozen=True, order=True)
class WeeklyContribution:
    """A Monday-anchored calendar week, possibly clipped by the date window."""

    week_start: date
    count: int


@dataclass(frozen=True)
class ContributionStats:
    total: int
    active_days: int
    best_week: int
    weekly: tuple[WeeklyContribution, ...]


def previous_complete_date_window(today: date | None = None) -> tuple[date, date]:
    """Return the previous 365 complete dates, excluding the current UTC date."""

    utc_today = today or datetime.now(timezone.utc).date()
    return utc_today - timedelta(days=DAYS_IN_WINDOW), utc_today - timedelta(days=1)


def _graphql_datetime(day: date, *, end_of_day: bool) -> str:
    suffix = "23:59:59Z" if end_of_day else "00:00:00Z"
    return f"{day.isoformat()}T{suffix}"


def _decode_graphql_response(raw: bytes) -> Mapping[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileStatsError(
            "GitHub returned a response that was not valid JSON"
        ) from exc

    if not isinstance(document, dict):
        raise ProfileStatsError("GitHub returned an unexpected GraphQL response")

    errors = document.get("errors")
    if errors:
        messages: list[str] = []
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    messages.append(error["message"])
        detail = "; ".join(messages) if messages else "unknown GraphQL error"
        raise ProfileStatsError(f"GitHub GraphQL error: {detail}")

    return document


def fetch_contribution_calendar(
    login: str,
    token: str,
    start: date,
    end: date,
    *,
    opener: Callable[..., Any] = urlopen,
) -> Mapping[str, Any]:
    """Request contribution-calendar data without ever placing the token in output."""

    if not login.strip():
        raise ProfileStatsError("GH_LOGIN must not be empty")
    if not token:
        raise ProfileStatsError("GITHUB_TOKEN must not be empty")
    if start > end:
        raise ValueError("the contribution date range is invalid")

    payload = {
        "query": CONTRIBUTIONS_QUERY,
        "variables": {
            "from": _graphql_datetime(start, end_of_day=False),
            "login": login,
            "to": _graphql_datetime(end, end_of_day=True),
        },
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    request = Request(
        GRAPHQL_ENDPOINT,
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-profile-contribution-generator",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )

    try:
        with opener(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        # Do not echo response bodies: concise status codes are actionable and cannot
        # accidentally reproduce credentials returned by an intermediary.
        raise ProfileStatsError(
            f"GitHub GraphQL request failed with HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise ProfileStatsError(
            "GitHub GraphQL request failed due to a network error"
        ) from exc

    return _decode_graphql_response(raw)


def extract_contribution_days(
    document: Mapping[str, Any], start: date, end: date
) -> tuple[ContributionDay, ...]:
    """Extract, filter, and strictly validate the requested daily calendar."""

    try:
        user = document["data"]["user"]
    except (KeyError, TypeError) as exc:
        raise ProfileStatsError(
            "GitHub returned an incomplete contribution calendar"
        ) from exc
    if user is None:
        raise ProfileStatsError("the configured GitHub user was not found")

    try:
        calendar = user["contributionsCollection"]["contributionCalendar"]
        weeks = calendar["weeks"]
    except (KeyError, TypeError) as exc:
        raise ProfileStatsError(
            "GitHub returned an incomplete contribution calendar"
        ) from exc
    if not isinstance(weeks, list):
        raise ProfileStatsError("GitHub returned an invalid contribution week list")

    counts_by_date: dict[date, int] = {}
    for week in weeks:
        if not isinstance(week, dict) or not isinstance(
            week.get("contributionDays"), list
        ):
            raise ProfileStatsError("GitHub returned an invalid contribution week")
        for raw_day in week["contributionDays"]:
            if not isinstance(raw_day, dict):
                raise ProfileStatsError("GitHub returned an invalid contribution day")
            raw_date = raw_day.get("date")
            count = raw_day.get("contributionCount")
            if not isinstance(raw_date, str):
                raise ProfileStatsError(
                    "GitHub returned a contribution day without a date"
                )
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ProfileStatsError("GitHub returned an invalid contribution count")
            try:
                parsed_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise ProfileStatsError(
                    f"GitHub returned an invalid contribution date: {raw_date}"
                ) from exc

            # Contribution calendars can include boundary cells outside the query.
            if parsed_date < start or parsed_date > end:
                continue
            if parsed_date in counts_by_date:
                raise ProfileStatsError(
                    f"GitHub returned duplicate data for {parsed_date.isoformat()}"
                )
            counts_by_date[parsed_date] = count

    expected_days = (end - start).days + 1
    ordered: list[ContributionDay] = []
    cursor = start
    while cursor <= end:
        if cursor not in counts_by_date:
            raise ProfileStatsError(
                "GitHub contribution calendar is missing "
                f"{cursor.isoformat()} (received {len(counts_by_date)} of {expected_days} dates)"
            )
        ordered.append(ContributionDay(cursor, counts_by_date[cursor]))
        cursor += timedelta(days=1)

    return tuple(ordered)


def aggregate_weekly(days: Sequence[ContributionDay]) -> tuple[WeeklyContribution, ...]:
    """Aggregate dates into stable Monday-through-Sunday calendar weeks."""

    totals: dict[date, int] = {}
    seen_dates: set[date] = set()
    for item in sorted(days, key=lambda value: value.day):
        if item.day in seen_dates:
            raise ValueError(f"duplicate contribution date: {item.day.isoformat()}")
        seen_dates.add(item.day)
        week_start = item.day - timedelta(days=item.day.weekday())
        totals[week_start] = totals.get(week_start, 0) + item.count

    return tuple(
        WeeklyContribution(week_start, totals[week_start])
        for week_start in sorted(totals)
    )


def calculate_stats(days: Sequence[ContributionDay]) -> ContributionStats:
    weekly = aggregate_weekly(days)
    return ContributionStats(
        total=sum(item.count for item in days),
        active_days=sum(1 for item in days if item.count > 0),
        best_week=max((item.count for item in weekly), default=0),
        weekly=weekly,
    )


def _format_coordinate(value: float) -> str:
    # A fixed precision keeps SVG output byte-for-byte stable across runs.
    if abs(value) < 0.0005:
        value = 0.0
    return f"{value:.1f}"


def _sparkline_points(
    values: Sequence[int],
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> tuple[tuple[float, float], ...]:
    if not values:
        return ((left, bottom), (right, bottom))

    peak = max(values)
    if len(values) == 1:
        y = bottom if peak == 0 else top
        return ((left, y), (right, y))

    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = left + ((right - left) * index / (len(values) - 1))
        y = bottom if peak == 0 else bottom - ((bottom - top) * value / peak)
        points.append((x, y))
    return tuple(points)


def _add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    next_year = year + 1 if month == 12 else year
    next_month = date(next_year, 1 if month == 12 else month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _quarterly_month_markers(start: date, end: date) -> tuple[tuple[date, str], ...]:
    """Return five quarterly labels that close on the starting month."""

    if start > end:
        raise ValueError("the contribution date range is invalid")

    first_month = date(start.year, start.month, 1)
    if first_month < start:
        first_month = _add_months(first_month, 1)

    markers = tuple(_add_months(first_month, offset) for offset in range(0, 13, 3))
    return tuple((marker, MONTH_LABELS[marker.month - 1]) for marker in markers)


def _smooth_curve_path(points: Sequence[tuple[float, float]]) -> str:
    """Return a shape-preserving cubic curve that passes through every point."""

    if len(points) < 2:
        raise ValueError("a sparkline requires at least two points")

    slopes = [
        (points[index + 1][1] - points[index][1])
        / (points[index + 1][0] - points[index][0])
        for index in range(len(points) - 1)
    ]
    tangents = [slopes[0]]
    for previous, following in pairwise(slopes):
        if previous == 0.0 or following == 0.0 or previous * following < 0.0:
            tangents.append(0.0)
        else:
            tangents.append((2.0 * previous * following) / (previous + following))
    tangents.append(slopes[-1])

    first_x, first_y = points[0]
    commands = [f"M {_format_coordinate(first_x)} {_format_coordinate(first_y)}"]
    for index, ((x1, y1), (x2, y2)) in enumerate(pairwise(points)):
        width = x2 - x1
        control_1 = (x1 + width / 3.0, y1 + tangents[index] * width / 3.0)
        control_2 = (
            x2 - width / 3.0,
            y2 - tangents[index + 1] * width / 3.0,
        )
        commands.append(
            "C "
            f"{_format_coordinate(control_1[0])} {_format_coordinate(control_1[1])} "
            f"{_format_coordinate(control_2[0])} {_format_coordinate(control_2[1])} "
            f"{_format_coordinate(x2)} {_format_coordinate(y2)}"
        )
    return " ".join(commands)


def render_svg(
    stats: ContributionStats,
    *,
    login: str,
    start: date,
    end: date,
) -> str:
    """Render a transparent, theme-aware and accessible SVG summary."""

    if start > end:
        raise ValueError("the contribution date range is invalid")

    window_days = (end - start).days + 1
    weekly_values = [item.count for item in stats.weekly]
    points = _sparkline_points(
        weekly_values, left=30.0, right=590.0, top=87.0, bottom=139.0
    )
    curve_path = _smooth_curve_path(points)
    area_path = f"{curve_path} L 590.0 139.0 L 30.0 139.0 Z"
    month_markers = _quarterly_month_markers(start, end)
    month_labels = "\n".join(
        f'  <text x="{_format_coordinate(30.0 + 560.0 * index / (len(month_markers) - 1))}" '
        f'y="159" text-anchor="middle" class="secondary label">'
        f"{html.escape(label, quote=True)}</text>"
        for index, (_, label) in enumerate(month_markers)
    )

    title = html.escape(
        f"{login} GitHub contribution summary for the last {window_days} complete UTC days",
        quote=True,
    )
    description = html.escape(
        f"{stats.total} contributions across {stats.active_days} active days from "
        f"{start.isoformat()} through {end.isoformat()}. The best Monday-anchored "
        f"week had {stats.best_week} contributions. Figures reflect contribution "
        "data available to the token used for generation.",
        quote=True,
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 620 166" role="img" aria-labelledby="contribution-title contribution-desc">
  <title id="contribution-title">{title}</title>
  <desc id="contribution-desc">{description}</desc>
  <style>
    text {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-variant-numeric: tabular-nums;
    }}
    .primary {{ fill: #171717; }}
    .secondary {{ fill: #737373; }}
    .baseline {{ stroke: #e5e5e5; }}
    .trend {{ fill: none; stroke: #3f3f46; }}
    .trend-fill {{ fill: #3f3f46; opacity: 0.06; }}
    .total {{ font-size: 46px; font-weight: 620; letter-spacing: -1.8px; }}
    .metric {{ font-size: 23px; font-weight: 560; letter-spacing: -0.6px; }}
    .label {{ font-size: 11px; font-weight: 450; }}
    @media (prefers-color-scheme: dark) {{
      .primary {{ fill: #f5f5f5; }}
      .secondary {{ fill: #a3a3a3; }}
      .baseline {{ stroke: #303030; }}
      .trend {{ stroke: #d4d4d4; }}
      .trend-fill {{ fill: #d4d4d4; opacity: 0.07; }}
    }}
  </style>

  <text x="30" y="59" class="primary total">{stats.total:,}</text>
  <text x="30" y="78" class="secondary label">contributions in the last year</text>

  <text x="454" y="51" text-anchor="middle" class="primary metric">{stats.active_days:,}</text>
  <text x="454" y="72" text-anchor="middle" class="secondary label">active days</text>
  <text x="554" y="51" text-anchor="middle" class="primary metric">{stats.best_week:,}</text>
  <text x="554" y="72" text-anchor="middle" class="secondary label">best week</text>

  <line x1="30" y1="139" x2="590" y2="139" class="baseline" vector-effect="non-scaling-stroke" />
  <path d="{area_path}" class="trend-fill" />
  <path d="{curve_path}" class="trend" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
{month_labels}
</svg>
"""


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a local SVG from GitHub contribution-calendar data."
    )
    parser.add_argument(
        "--login",
        default=os.environ.get("GH_LOGIN"),
        help="GitHub login (defaults to GH_LOGIN)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output path (default: {DEFAULT_OUTPUT.as_posix()})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    login = args.login
    token = os.environ.get("GITHUB_TOKEN", "")

    if not login:
        print("error: set GH_LOGIN or pass --login", file=sys.stderr)
        return 2
    if not token:
        print("error: set GITHUB_TOKEN for the GitHub GraphQL API", file=sys.stderr)
        return 2

    try:
        start, end = previous_complete_date_window()
        document = fetch_contribution_calendar(login, token, start, end)
        days = extract_contribution_days(document, start, end)
        svg = render_svg(calculate_stats(days), login=login, start=start, end=end)
        changed = write_if_changed(args.output, svg)
    except (OSError, ProfileStatsError, TypeError, ValueError) as exc:
        # Defensive redaction also covers unexpected upstream error messages.
        message = str(exc).replace(token, "[redacted]") if token else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1

    state = "updated" if changed else "already up to date"
    print(f"Contribution graphic {state}: {args.output.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
