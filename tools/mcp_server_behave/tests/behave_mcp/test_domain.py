"""Plain unit tests for pure domain logic."""

import pytest

from behave_mcp import domain
from behave_mcp.messages import JobRecord


def test_classify_job_state_live_handle_running():
    result = domain.classify_job_state(
        has_live_handle=True,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "running"
    assert result.ok is None
    assert result.reason == "live_handle_running"


def test_classify_job_state_live_handle_exited_ok():
    result = domain.classify_job_state(
        has_live_handle=True,
        returncode=0,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is True
    assert result.reason == "live_handle_exited"


def test_classify_job_state_live_handle_exited_failed():
    result = domain.classify_job_state(
        has_live_handle=True,
        returncode=1,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is False
    assert result.reason == "live_handle_exited"


def test_classify_job_state_recovered_report_present_ok():
    result = domain.classify_job_state(
        has_live_handle=False,
        returncode=None,
        report_present=True,
        report_ok=True,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "completed"
    assert result.ok is True
    assert result.reason == "report_present"


def test_classify_job_state_recovered_report_present_failed():
    result = domain.classify_job_state(
        has_live_handle=False,
        returncode=None,
        report_present=True,
        report_ok=False,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "completed"
    assert result.ok is False
    assert result.reason == "report_present"


def test_classify_job_state_recovered_pid_alive_no_report():
    result = domain.classify_job_state(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=True,
    )
    assert result.status == "running"
    assert result.ok is None
    assert result.reason == "pid_alive_no_report"


def test_classify_job_state_recovered_pid_dead_no_report():
    result = domain.classify_job_state(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=123,
        pid_alive=False,
    )
    assert result.status == "unknown"
    assert result.ok is False
    assert result.reason == "pid_dead_no_report"


def test_classify_job_state_recovered_pid_unknown_no_report():
    result = domain.classify_job_state(
        has_live_handle=False,
        returncode=None,
        report_present=False,
        report_ok=None,
        pid=None,
        pid_alive=False,
    )
    assert result.status == "unknown"
    assert result.ok is False
    assert result.reason == "pid_unknown_no_report"


# ---- job_matches_result_filters ----


def test_job_matches_result_filters_no_filters_matches_everything():
    assert domain.job_matches_result_filters(
        JobRecord(),
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_job_ids():
    metadata = JobRecord()
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids={"job1", "job2"},
        feature_file=None,
        scenario_name=None,
        release=None,
        machine_type=None,
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job3",
        job_ids={"job1", "job2"},
        feature_file=None,
        scenario_name=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_feature_file():
    metadata = JobRecord(feature_file="features/cli/attach.feature")
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file="features/cli/attach.feature",
        scenario_name=None,
        release=None,
        machine_type=None,
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file="features/cli/other.feature",
        scenario_name=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_scenario_name_substring():
    metadata = JobRecord(scenario_name="Attach invalid token")
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name="invalid",
        release=None,
        machine_type=None,
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name="expired",
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_release_and_machine_type():
    metadata = JobRecord(releases=["jammy"], machine_types=["lxd-container"])
    assert domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release="jammy",
        machine_type="lxd-container",
    )
    assert not domain.job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release="resolute",
        machine_type=None,
    )


# ---- scenario_status_from_element ----


def test_scenario_status_from_element_prefers_reported_status():
    scenario = {
        "status": "skipped",
        "steps": [],
    }
    assert domain.scenario_status_from_element(scenario) == "skipped"


def test_scenario_status_from_element_maps_error_statuses_to_failed():
    for reported_status in ("failed", "error", "undefined", "hook_error"):
        scenario = {"status": reported_status, "steps": []}
        assert domain.scenario_status_from_element(scenario) == "failed"


def test_scenario_status_from_element_falls_back_to_steps_when_absent():
    scenario = {
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }
    assert domain.scenario_status_from_element(scenario) == "passed"


def test_scenario_status_from_element_falls_back_for_unknown_reported_status():
    scenario = {
        "status": "some-future-behave-status",
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }
    assert domain.scenario_status_from_element(scenario) == "passed"


# ---- summarize_report ----


def test_summarize_report_classifies_skipped_scenarios_correctly():
    report_data = [
        {
            "name": "feature",
            "elements": [
                {
                    "name": "passing scenario",
                    "status": "passed",
                    "steps": [
                        {"name": "step", "result": {"status": "passed"}}
                    ],
                },
                {
                    "name": "skipped scenario",
                    "status": "skipped",
                    "steps": [],
                },
            ],
        }
    ]

    result = domain.summarize_report(report_data)

    assert result.summary["scenarios"]["passed"] == 1
    assert result.summary["scenarios"]["skipped"] == 1
    assert result.summary["scenarios"]["unknown"] == 0


# ---- grouped_counts_from_report / merge_grouped_counts / grouped_counts ----


def _scenario_element(location, status, name="scenario"):
    return {
        "name": name,
        "location": location,
        "steps": [{"name": "step", "result": {"status": status}}],
    }


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
        got = domain.select_log_lines(self.LINES, limit=2)

        assert got.text == self.numbered(9, 10)
        assert (got.first_line, got.last_line) == (9, 10)
        assert got.truncated is True
        assert got.matches == 0

    def test_a_start_reads_forward_from_that_line(self):
        got = domain.select_log_lines(self.LINES, start=3, limit=3)

        assert got.text == self.numbered(3, 4, 5)
        assert got.truncated is True

    def test_a_range_that_reaches_the_end_is_not_truncated(self):
        got = domain.select_log_lines(self.LINES, start=9, limit=50)

        assert (got.first_line, got.last_line) == (9, 10)
        assert got.truncated is False

    def test_a_start_past_the_end_returns_nothing(self):
        got = domain.select_log_lines(self.LINES, start=99, limit=5)

        assert got.text == ""
        assert (got.first_line, got.last_line) == (None, None)

    def test_an_empty_log_returns_nothing(self):
        assert domain.select_log_lines([], limit=5).text == ""

    def test_matches_come_with_context_and_grep_separators(self):
        got = domain.select_log_lines(
            self.LINES, pattern="^errored|^traceback", context=1, limit=50
        )

        assert got.text == (
            self.numbered(2, 3, 4) + "\n--\n" + self.numbered(8, 9, 10)
        )
        assert got.matches == 2
        assert (got.first_line, got.last_line) == (2, 10)

    def test_overlapping_windows_merge(self):
        got = domain.select_log_lines(
            self.LINES, pattern="KeyError|HOOK-ERROR", context=1, limit=50
        )

        # Lines 5 and 7 match; their windows share line 6.
        assert got.text == self.numbered(4, 5, 6, 7, 8)
        assert got.matches == 2

    def test_the_line_budget_stops_before_a_window_would_exceed_it(self):
        got = domain.select_log_lines(
            self.LINES, pattern="^errored|^traceback", context=1, limit=4
        )

        assert got.text == self.numbered(2, 3, 4)
        assert got.matches == 2
        assert got.truncated is True

    def test_search_starts_from_the_given_line(self):
        got = domain.select_log_lines(
            self.LINES, pattern="error", context=0, start=6, limit=50
        )

        assert got.text == self.numbered(7) + "\n--\n" + self.numbered(9)
        assert got.matches == 2

    def test_no_match_reports_nothing_found(self):
        got = domain.select_log_lines(self.LINES, pattern="zzz", limit=50)

        assert got.text == ""
        assert got.matches == 0
        assert got.truncated is False

    def test_an_invalid_pattern_is_rejected(self):
        with pytest.raises(ValueError, match="invalid pattern"):
            domain.select_log_lines(self.LINES, pattern="(", limit=5)


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

        digest = domain.digest_log(lines)

        (region,) = digest.errors
        assert region.kind == "traceback"
        assert (region.first_line, region.last_line) == (3, 8)
        assert region.step == domain.StepRef(2, self.STEP_ERROR.strip())
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

        (region,) = domain.digest_log(lines).errors

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

        (region,) = domain.digest_log(lines).errors

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

        (region,) = domain.digest_log(lines).errors

        assert region.kind == "assert"
        assert (region.first_line, region.last_line) == (2, 11)
        assert region.exception == "AssertionError: Expected to match regexp:"
        assert region.step is not None and region.step.line == 1

    def test_the_summary_and_tail_come_along(self):
        lines = ["tox preamble", *self.SUMMARY, "behave: exit 1"]

        digest = domain.digest_log(lines)

        assert digest.finished is True
        assert digest.summary is not None
        assert (digest.summary.first_line, digest.summary.last_line) == (2, 6)
        assert digest.summary.text == "\n".join(self.SUMMARY)
        assert digest.tail == "\n".join(lines[-5:])
        assert digest.errors == []

    def test_a_passing_run_has_a_counts_only_summary(self):
        lines = ["3 features passed, 0 failed, 0 skipped", "Took 1min 2s"]

        digest = domain.digest_log(lines)

        assert digest.summary is not None
        assert digest.summary.first_line == 1

    def test_a_run_that_never_reached_behave_is_not_finished(self):
        digest = domain.digest_log(["pip: error", "tox: exit 1"])

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

        (region,) = domain.digest_log(lines).errors

        body = region.text.splitlines()
        assert (
            len(body)
            == domain.DIGEST_HEAD_LINES + 1 + domain.DIGEST_TAIL_LINES
        )
        assert "[%d lines omitted]" % (61 - 33) in region.text
        assert all(
            len(line) <= domain.DIGEST_MAX_LINE_CHARS + 2 for line in body
        )

    def test_regions_are_capped_but_counted(self):
        lines = []
        for _ in range(domain.DIGEST_MAX_REGIONS + 2):
            lines.extend([*self.TRACEBACK, ""])

        digest = domain.digest_log(lines)

        assert len(digest.errors) == domain.DIGEST_MAX_REGIONS
        assert digest.errors_total == domain.DIGEST_MAX_REGIONS + 2
