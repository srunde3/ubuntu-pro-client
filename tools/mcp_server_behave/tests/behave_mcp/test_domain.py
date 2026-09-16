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


def test_grouped_counts_from_report_classifies_skipped_scenarios_correctly():
    """A skipped scenario has no executed steps, only a scenario-level

    ``status`` -- regression test for the bug where such scenarios were
    mis-bucketed as "unknown" instead of "skipped".
    """
    report_data = [
        {
            "name": "feature",
            "elements": [
                {
                    "name": "scenario",
                    "location": "features/f.feature:10",
                    "status": "skipped",
                    "steps": [],
                },
            ],
        }
    ]

    by_release, _ = domain.grouped_counts_from_report(
        report_data, ["jammy"], []
    )

    assert by_release["jammy"]["skipped"] == 1
    assert by_release["jammy"]["unknown"] == 0


def test_grouped_counts_from_report_attributes_to_all_declared_values():
    report_data = [
        {
            "name": "feature",
            "elements": [
                _scenario_element("features/f.feature:10", "passed"),
                _scenario_element("features/f.feature:11", "failed"),
            ],
        }
    ]

    by_release, by_machine_type = domain.grouped_counts_from_report(
        report_data, ["jammy", "noble"], ["lxd-container"]
    )

    assert by_release["jammy"] == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "unknown": 0,
    }
    assert by_release["noble"]["total"] == 2
    assert by_machine_type["lxd-container"]["passed"] == 1
    assert by_machine_type["lxd-container"]["failed"] == 1


def test_merge_grouped_counts_sums_across_jobs():
    target: dict = {}
    domain.merge_grouped_counts(
        target,
        {
            "jammy": {
                "total": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "unknown": 0,
            }
        },
    )
    domain.merge_grouped_counts(
        target,
        {
            "jammy": {
                "total": 1,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "unknown": 0,
            }
        },
    )

    assert target["jammy"]["total"] == 2
    assert target["jammy"]["passed"] == 1
    assert target["jammy"]["failed"] == 1


def test_grouped_counts_from_dict_sorted_by_name():
    counts = {
        "resolute": {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "jammy": {
            "total": 2,
            "passed": 1,
            "failed": 1,
            "skipped": 0,
            "unknown": 0,
        },
    }

    result = domain.grouped_counts_from_dict(counts)

    assert [g.name for g in result] == ["jammy", "resolute"]
    assert result[1].total == 1


# ---- job_failures_from_report ----


def test_job_failures_from_report_tags_job_and_declared_context():
    report_data = [
        {
            "name": "my-feature",
            "elements": [
                {
                    "name": "my-scenario",
                    "location": "features/f.feature:10",
                    "steps": [
                        {
                            "name": "a failing step",
                            "result": {
                                "status": "failed",
                                "error_message": "boom",
                            },
                        }
                    ],
                }
            ],
        }
    ]

    failures = domain.job_failures_from_report(
        report_data, "job1", ["jammy"], ["lxd-container", "lxd-vm"]
    )

    assert len(failures) == 1
    failure = failures[0]
    assert failure.feature == "my-feature"
    assert failure.scenario == "my-scenario"
    assert failure.step == "a failing step"
    assert failure.job_id == "job1"
    assert failure.releases == ["jammy"]
    assert failure.machine_types == ["lxd-container", "lxd-vm"]


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
