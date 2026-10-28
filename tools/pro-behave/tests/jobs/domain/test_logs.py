"""Selecting log lines."""

import pytest

from pro_behave.jobs.domain.logs import select_log_lines


class TestSelectLogLines:
    LINES = [
        "tox preamble",
        "Given a machine ... error in 0.1s",
        "Traceback (most recent call last):",
        '  File "steps.py", line 4, in given',
        "KeyError: 'base'",
        "",
        "HOOK-ERROR in after_all: InstanceNotFoundError",
        '  File "environment.py", line 684, in after_all',
        "Errored scenarios:",
        "Took 0min 0.002s",
    ]

    def numbered(self, *numbers):
        return "\n".join(
            "{}: {}".format(n, self.LINES[n - 1]) for n in numbers
        )

    def test_no_pattern_no_start_is_the_tail(self):
        got = select_log_lines(self.LINES, limit=2)

        assert got.text == self.numbered(9, 10)
        assert (got.first_line, got.last_line) == (9, 10)
        assert got.truncated is True
        assert got.matches == 0

    def test_a_start_reads_forward_from_that_line(self):
        got = select_log_lines(self.LINES, start=3, limit=3)

        assert got.text == self.numbered(3, 4, 5)
        assert got.truncated is True

    def test_a_range_that_reaches_the_end_is_not_truncated(self):
        got = select_log_lines(self.LINES, start=9, limit=50)

        assert (got.first_line, got.last_line) == (9, 10)
        assert got.truncated is False

    def test_a_start_past_the_end_returns_nothing(self):
        got = select_log_lines(self.LINES, start=99, limit=5)

        assert got.text == ""
        assert (got.first_line, got.last_line) == (None, None)

    def test_an_empty_log_returns_nothing(self):
        assert select_log_lines([], limit=5).text == ""

    def test_matches_come_with_context_and_grep_separators(self):
        got = select_log_lines(
            self.LINES, pattern="^errored|^traceback", context=1, limit=50
        )

        assert got.text == (
            self.numbered(2, 3, 4) + "\n--\n" + self.numbered(8, 9, 10)
        )
        assert got.matches == 2
        assert (got.first_line, got.last_line) == (2, 10)

    def test_overlapping_windows_merge(self):
        got = select_log_lines(
            self.LINES, pattern="KeyError|HOOK-ERROR", context=1, limit=50
        )

        # Lines 5 and 7 match; their windows share line 6.
        assert got.text == self.numbered(4, 5, 6, 7, 8)
        assert got.matches == 2

    def test_the_line_budget_stops_before_a_window_would_exceed_it(self):
        got = select_log_lines(
            self.LINES, pattern="^errored|^traceback", context=1, limit=4
        )

        assert got.text == self.numbered(2, 3, 4)
        assert got.matches == 2
        assert got.truncated is True

    def test_search_starts_from_the_given_line(self):
        got = select_log_lines(
            self.LINES, pattern="error", context=0, start=6, limit=50
        )

        assert got.text == self.numbered(7) + "\n--\n" + self.numbered(9)
        assert got.matches == 2

    def test_no_match_reports_nothing_found(self):
        got = select_log_lines(self.LINES, pattern="zzz", limit=50)

        assert got.text == ""
        assert got.matches == 0
        assert got.truncated is False

    def test_an_invalid_pattern_is_rejected(self):
        with pytest.raises(ValueError, match="invalid pattern"):
            select_log_lines(self.LINES, pattern="(", limit=5)
