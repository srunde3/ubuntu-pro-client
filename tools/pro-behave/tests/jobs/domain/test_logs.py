"""Selecting log lines and digesting a job's errors."""

import pytest

from pro_behave.jobs.domain.logs import (
    DIGEST_HEAD_LINES,
    DIGEST_MAX_LINE_CHARS,
    DIGEST_MAX_REGIONS,
    DIGEST_TAIL_LINES,
    StepRef,
    digest_log,
    select_log_lines,
)


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


class TestDigestLog:
    """Failure regions, in order, from behave's plain-formatter output."""

    STEP_ERROR = "    Given a `bionic` `wsl` machine ... error in 0.002s"
    TRACEBACK = [
        "Traceback (most recent call last):",
        '  File "behave/model.py", line 1991, in run',
        "    match.run(runner.context)",
        '  File "features/steps/machines.py", line 44, in given',
        "    raise KeyError('base must be defined')",
        "KeyError: 'base must be defined'",
    ]
    SUMMARY = [
        "Errored scenarios:",
        "  features/cli/detach.feature:141  Attached detach",
        "",
        "0 features passed, 0 failed, 1 error, 0 skipped",
        "Took 0min 0.002s",
    ]

    def test_a_traceback_is_tied_to_the_failing_step_above_it(self):
        lines = ["  Scenario: x", self.STEP_ERROR, *self.TRACEBACK, ""]

        digest = digest_log(lines)

        (region,) = digest.errors
        assert region.kind == "traceback"
        assert (region.first_line, region.last_line) == (3, 8)
        assert region.step == StepRef(2, self.STEP_ERROR.strip())
        assert region.exception == "KeyError: 'base must be defined'"
        assert region.text == "\n".join(self.TRACEBACK)

    def test_a_new_scenario_clears_the_step(self):
        lines = [
            self.STEP_ERROR,
            "",
            "  Scenario Outline: another -- @1.4",
            "",
            "HOOK-ERROR in after_all: InstanceNotFoundError: gone",
            '  File "features/environment.py", line 684, in after_all',
            "",
        ]

        (region,) = digest_log(lines).errors

        assert region.kind == "hook_error"
        assert region.step is None
        assert region.exception.startswith("HOOK-ERROR in after_all")
        assert (region.first_line, region.last_line) == (5, 6)

    def test_a_chained_traceback_is_one_region_naming_the_last_raised(self):
        lines = [
            "Traceback (most recent call last):",
            '  File "azure.py", line 1, in create',
            "    raise error",
            "azure.ResourceNotFoundError: (PlatformImageNotFound) no image",
            "Code: PlatformImageNotFound",
            "Target: imageReference",
            "",
            "The above exception was the direct cause of the following "
            "exception:",
            "",
            "Traceback (most recent call last):",
            '  File "pycloudlib.py", line 2, in launch',
            "    raise PycloudlibError('creation error') from e",
            "pycloudlib.errors.PycloudlibError: creation error",
            "",
        ]

        (region,) = digest_log(lines).errors

        assert (region.first_line, region.last_line) == (1, 13)
        assert region.exception == (
            "pycloudlib.errors.PycloudlibError: creation error"
        )

    def test_an_assertion_and_its_traceback_are_one_region(self):
        lines = [
            "    Then output matches ... failed in 0.001s",
            "ASSERT FAILED: Expected to match regexp:",
            "  {",
            "But got:",
            "  {",
            "",
            "Traceback (most recent call last):",
            '  File "steps.py", line 9, in then',
            "    assert False",
            "AssertionError: Expected to match regexp:",
            "  {",
            "",
        ]

        (region,) = digest_log(lines).errors

        assert region.kind == "assert"
        assert (region.first_line, region.last_line) == (2, 11)
        assert region.exception == "AssertionError: Expected to match regexp:"
        assert region.step is not None and region.step.line == 1

    def test_the_summary_and_tail_come_along(self):
        lines = ["tox preamble", *self.SUMMARY, "behave: exit 1"]

        digest = digest_log(lines)

        assert digest.finished is True
        assert digest.summary is not None
        assert (digest.summary.first_line, digest.summary.last_line) == (2, 6)
        assert digest.summary.text == "\n".join(self.SUMMARY)
        assert digest.tail == "\n".join(lines[-5:])
        assert digest.errors == []

    def test_a_passing_run_has_a_counts_only_summary(self):
        lines = ["3 features passed, 0 failed, 0 skipped", "Took 1min 2s"]

        digest = digest_log(lines)

        assert digest.summary is not None
        assert digest.summary.first_line == 1

    def test_a_run_that_never_reached_behave_is_not_finished(self):
        digest = digest_log(["pip: error", "tox: exit 1"])

        assert digest.finished is False
        assert digest.errors == []
        assert digest.tail == "pip: error\ntox: exit 1"

    def test_long_regions_and_lines_are_elided(self):
        json_lines = ["\t%s" % ("x" * 500) for _ in range(60)]
        lines = [
            "HOOK-ERROR in after_step: Timeout stdout: {",
            *json_lines,
            "",
        ]

        (region,) = digest_log(lines).errors

        body = region.text.splitlines()
        assert len(body) == DIGEST_HEAD_LINES + 1 + DIGEST_TAIL_LINES
        assert "[%d lines omitted]" % (61 - 33) in region.text
        assert all(len(line) <= DIGEST_MAX_LINE_CHARS + 2 for line in body)

    def test_regions_are_capped_but_counted(self):
        lines = []
        for _ in range(DIGEST_MAX_REGIONS + 2):
            lines.extend([*self.TRACEBACK, ""])

        digest = digest_log(lines)

        assert len(digest.errors) == DIGEST_MAX_REGIONS
        assert digest.errors_total == DIGEST_MAX_REGIONS + 2
