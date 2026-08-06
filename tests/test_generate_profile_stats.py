from __future__ import annotations

import io
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import generate_profile_stats as profile_stats


class AggregationTests(unittest.TestCase):
    def test_weekly_aggregation_is_monday_anchored_and_sorted(self) -> None:
        days = [
            profile_stats.ContributionDay(date(2026, 1, 5), 7),  # Monday
            profile_stats.ContributionDay(date(2026, 1, 4), 3),  # Sunday
            profile_stats.ContributionDay(date(2026, 1, 1), 2),  # Thursday
        ]

        weekly = profile_stats.aggregate_weekly(days)

        self.assertEqual(
            weekly,
            (
                profile_stats.WeeklyContribution(date(2025, 12, 29), 5),
                profile_stats.WeeklyContribution(date(2026, 1, 5), 7),
            ),
        )

    def test_calculates_total_active_days_and_best_week(self) -> None:
        days = [
            profile_stats.ContributionDay(date(2026, 2, 2), 4),
            profile_stats.ContributionDay(date(2026, 2, 3), 0),
            profile_stats.ContributionDay(date(2026, 2, 8), 6),
            profile_stats.ContributionDay(date(2026, 2, 9), 8),
        ]

        result = profile_stats.calculate_stats(days)

        self.assertEqual(result.total, 18)
        self.assertEqual(result.active_days, 3)
        self.assertEqual(result.best_week, 10)
        self.assertEqual([week.count for week in result.weekly], [10, 8])

    def test_empty_and_zero_contribution_inputs(self) -> None:
        empty = profile_stats.calculate_stats([])
        zero = profile_stats.calculate_stats(
            [
                profile_stats.ContributionDay(date(2026, 3, 2), 0),
                profile_stats.ContributionDay(date(2026, 3, 3), 0),
            ]
        )

        self.assertEqual(empty, profile_stats.ContributionStats(0, 0, 0, ()))
        self.assertEqual(zero.total, 0)
        self.assertEqual(zero.active_days, 0)
        self.assertEqual(zero.best_week, 0)
        self.assertEqual([week.count for week in zero.weekly], [0])

    def test_duplicate_dates_are_rejected(self) -> None:
        duplicate = profile_stats.ContributionDay(date(2026, 1, 1), 1)

        with self.assertRaisesRegex(ValueError, "duplicate contribution date"):
            profile_stats.aggregate_weekly([duplicate, duplicate])


class CalendarParsingTests(unittest.TestCase):
    @staticmethod
    def _payload(raw_days: list[dict[str, object]]) -> dict[str, object]:
        return {
            "data": {
                "user": {
                    "contributionsCollection": {
                        "contributionCalendar": {
                            "weeks": [{"contributionDays": raw_days}]
                        }
                    }
                }
            }
        }

    def test_extracts_exact_range_and_ignores_boundary_cells(self) -> None:
        payload = self._payload(
            [
                {"date": "2026-03-31", "contributionCount": 99},
                {"date": "2026-04-01", "contributionCount": 2},
                {"date": "2026-04-02", "contributionCount": 0},
                {"date": "2026-04-03", "contributionCount": 4},
                {"date": "2026-04-04", "contributionCount": 99},
            ]
        )

        days = profile_stats.extract_contribution_days(
            payload, date(2026, 4, 1), date(2026, 4, 3)
        )

        self.assertEqual([item.count for item in days], [2, 0, 4])

    def test_missing_calendar_date_fails_clearly(self) -> None:
        payload = self._payload([{"date": "2026-04-01", "contributionCount": 2}])

        with self.assertRaisesRegex(
            profile_stats.ProfileStatsError, "missing 2026-04-02"
        ):
            profile_stats.extract_contribution_days(
                payload, date(2026, 4, 1), date(2026, 4, 2)
            )

    def test_invalid_date_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "date range is invalid"):
            profile_stats.extract_contribution_days(
                self._payload([]), date(2026, 4, 2), date(2026, 4, 1)
            )

    def test_missing_user_fails_clearly(self) -> None:
        with self.assertRaisesRegex(
            profile_stats.ProfileStatsError, "configured GitHub user was not found"
        ):
            profile_stats.extract_contribution_days(
                {"data": {"user": None}}, date(2026, 4, 1), date(2026, 4, 1)
            )

    def test_graphql_errors_fail_clearly(self) -> None:
        response = b'{"errors":[{"message":"Resource not accessible"}]}'

        with self.assertRaisesRegex(
            profile_stats.ProfileStatsError,
            "GitHub GraphQL error: Resource not accessible",
        ):
            profile_stats._decode_graphql_response(response)

    def test_previous_window_contains_365_complete_dates(self) -> None:
        start, end = profile_stats.previous_complete_date_window(date(2026, 7, 31))

        self.assertEqual(start, date(2025, 7, 31))
        self.assertEqual(end, date(2026, 7, 30))
        self.assertEqual((end - start).days + 1, 365)


class SvgTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = date(2026, 1, 1)
        self.end = date(2026, 1, 14)
        self.days = [
            profile_stats.ContributionDay(date(2026, 1, 1), 2),
            profile_stats.ContributionDay(date(2026, 1, 2), 0),
            profile_stats.ContributionDay(date(2026, 1, 8), 5),
        ]

    def test_svg_output_is_deterministic(self) -> None:
        first_stats = profile_stats.calculate_stats(self.days)
        second_stats = profile_stats.calculate_stats(list(reversed(self.days)))

        first = profile_stats.render_svg(
            first_stats, login="example", start=self.start, end=self.end
        )
        second = profile_stats.render_svg(
            second_stats, login="example", start=self.start, end=self.end
        )

        self.assertEqual(first, second)
        self.assertIn('<path d="M 30.0', first)
        self.assertNotIn("generated at", first.lower())

    def test_svg_is_valid_xml_and_has_accessible_metadata(self) -> None:
        svg = profile_stats.render_svg(
            profile_stats.calculate_stats(self.days),
            login="example",
            start=self.start,
            end=self.end,
        )

        root = ET.fromstring(svg)
        namespace = "{http://www.w3.org/2000/svg}"

        self.assertEqual(root.tag, f"{namespace}svg")
        self.assertIsNotNone(root.find(f"{namespace}title"))
        self.assertIsNotNone(root.find(f"{namespace}desc"))
        self.assertEqual(root.attrib["viewBox"], "0 0 620 166")
        self.assertIn("@media (prefers-color-scheme: dark)", svg)

    def test_generated_xml_text_is_escaped(self) -> None:
        login = 'A&B <team> "quoted"'

        svg = profile_stats.render_svg(
            profile_stats.calculate_stats(self.days),
            login=login,
            start=self.start,
            end=self.end,
        )
        root = ET.fromstring(svg)
        title_text = root.findtext("{http://www.w3.org/2000/svg}title")

        self.assertIsNotNone(title_text)
        self.assertIn(login, title_text or "")
        self.assertIn("A&amp;B &lt;team&gt; &quot;quoted&quot;", svg)
        self.assertNotIn("A&B <team>", svg)

    def test_minimal_layout_contains_only_requested_visible_content(self) -> None:
        svg = profile_stats.render_svg(
            profile_stats.calculate_stats(self.days),
            login="example",
            start=self.start,
            end=self.end,
        )

        for label in (
            "contributions in the last year",
            "active days",
            "best week",
            "Jan",
        ):
            self.assertIn(label, svg)
        for clutter in (
            "CONTRIBUTION LEDGER",
            "WEEKLY CONTRIBUTION SIGNAL",
            "CALENDAR WEEKS",
            "<circle",
            "<polygon",
            "<polyline",
        ):
            self.assertNotIn(clutter, svg)
        self.assertEqual(svg.count("<line "), 1)
        self.assertEqual(svg.count("font-size:"), 3)

    def test_only_graph_animates_for_two_seconds(self) -> None:
        svg = profile_stats.render_svg(
            profile_stats.calculate_stats(self.days),
            login="example",
            start=self.start,
            end=self.end,
        )

        self.assertIn(
            "animation: graph-sweep 2000ms cubic-bezier(.4, 0, .2, 1) both",
            svg,
        )
        self.assertEqual(svg.count("animation:"), 2)
        self.assertNotIn("animation-delay", svg)
        self.assertNotIn("stat-in", svg)
        self.assertNotIn("month-in", svg)
        self.assertNotIn('pathLength="1"', svg)
        self.assertNotIn("stroke-dasharray", svg)
        self.assertIn("prefers-reduced-motion: reduce", svg)

    def test_month_labels_are_spaced_every_three_months(self) -> None:
        markers = profile_stats._quarterly_month_markers(
            date(2025, 7, 31), date(2026, 7, 30)
        )

        self.assertEqual(
            markers,
            (
                (date(2025, 8, 1), "Aug"),
                (date(2025, 11, 1), "Nov"),
                (date(2026, 2, 1), "Feb"),
                (date(2026, 5, 1), "May"),
                (date(2026, 8, 1), "Aug"),
            ),
        )

        svg = profile_stats.render_svg(
            profile_stats.calculate_stats(self.days),
            login="example",
            start=date(2025, 7, 31),
            end=date(2026, 7, 30),
        )
        root = ET.fromstring(svg)
        month_nodes = root.findall(".//{http://www.w3.org/2000/svg}text")[-5:]
        self.assertEqual(
            [node.text for node in month_nodes], ["Aug", "Nov", "Feb", "May", "Aug"]
        )
        self.assertEqual(
            [node.attrib["x"] for node in month_nodes],
            ["30.0", "170.0", "310.0", "450.0", "590.0"],
        )

    def test_smooth_curve_passes_through_weekly_points(self) -> None:
        points = ((0.0, 4.0), (10.0, 1.0), (20.0, 6.0))

        path = profile_stats._smooth_curve_path(points)

        self.assertTrue(path.startswith("M 0.0 4.0"))
        self.assertIn("10.0 1.0", path)
        self.assertTrue(path.endswith("20.0 6.0"))

    def test_zero_stats_still_render_a_readable_signal(self) -> None:
        svg = profile_stats.render_svg(
            profile_stats.ContributionStats(0, 0, 0, ()),
            login="example",
            start=self.start,
            end=self.end,
        )

        ET.fromstring(svg)
        self.assertIn(
            'd="M 30.0 139.0 C 216.7 139.0 403.3 139.0 590.0 139.0"',
            svg,
        )
        self.assertIn(">0</text>", svg)


class FileOutputTests(unittest.TestCase):
    def test_write_if_changed_reports_only_content_changes(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "generated" / "contributions.svg"

            self.assertTrue(profile_stats.write_if_changed(output, "first\n"))
            self.assertFalse(profile_stats.write_if_changed(output, "first\n"))
            self.assertTrue(profile_stats.write_if_changed(output, "second\n"))
            self.assertEqual(output.read_text(encoding="utf-8"), "second\n")


class ErrorSafetyTests(unittest.TestCase):
    def test_main_redacts_token_from_unexpected_error_text(self) -> None:
        token = "github_token_that_must_not_be_printed"
        stderr = io.StringIO()

        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": token, "GH_LOGIN": "example"},
                clear=True,
            ),
            patch.object(
                profile_stats,
                "fetch_contribution_calendar",
                side_effect=profile_stats.ProfileStatsError(f"upstream echoed {token}"),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = profile_stats.main([])

        self.assertEqual(exit_code, 1)
        self.assertNotIn(token, stderr.getvalue())
        self.assertIn("[redacted]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
