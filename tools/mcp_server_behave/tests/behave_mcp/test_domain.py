"""Plain unit tests for pure domain logic."""

import subprocess
import sys

import pytest

from behave_mcp import domain
from behave_mcp.messages import JobRecord
from behave_mcp.pools import Pool


def test_build_command_escapes_scenario_name_for_behave_regex(tmp_path):
    command = domain.build_command(
        "features/proxy_config.feature",
        ["lxd-container"],
        "apt_http(s)_proxy still works",
        ["bionic"],
        tmp_path / "report.json",
    )

    assert command[command.index("--name") + 1] == (
        r"^apt_http\(s\)_proxy\ still\ works(?: -- @.*)?$"
    )


@pytest.mark.integration
def test_exact_name_pattern_selects_one_scenario_and_its_rows(tmp_path):
    features = tmp_path / "features"
    (features / "steps").mkdir(parents=True)
    (features / "steps" / "steps.py").write_text(
        "from behave import given\n"
        "@given('step {x}')\n"
        "def step(context, x):\n"
        "    pass\n"
    )
    (features / "x.feature").write_text(
        "Feature: x\n"
        "  Scenario Outline: Attach command\n"
        "    Given step <release>\n"
        "    Examples: ubuntu release\n"
        "      | release |\n"
        "      | jammy   |\n"
        "      | noble   |\n"
        "\n"
        "  Scenario Outline: Attach command with token\n"
        "    Given step <release>\n"
        "    Examples:\n"
        "      | release |\n"
        "      | jammy   |\n"
        "\n"
        "  Scenario: Attach command (plain)\n"
        "    Given step x\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "behave",
            "--dry-run",
            "--no-summary",
            "-f",
            "plain",
            "--name",
            domain.exact_name_pattern("Attach command"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    selected = [
        line.strip()
        for line in result.stdout.splitlines()
        if "Scenario" in line
    ]
    assert selected == [
        "Scenario Outline: Attach command -- @1.1 ubuntu release",
        "Scenario Outline: Attach command -- @1.2 ubuntu release",
    ]


# Any pid: classify_job_state cares only whether one was recorded.
RECORDED_PID = 4321


def _signals(
    *,
    live=False,
    returncode=None,
    report=None,
    pid=None,
    alive=False,
):
    """``classify_job_state`` inputs; ``report`` is None, True or False."""
    return dict(
        has_live_handle=live,
        returncode=returncode,
        report_present=report is not None,
        report_ok=report,
        pid=pid,
        pid_alive=alive,
    )


JOB_STATE_CASES = [
    pytest.param(
        _signals(live=True, pid=RECORDED_PID, alive=True),
        ("running", None, domain.ClassifyReason.LIVE_HANDLE_RUNNING),
        id="live-handle-running",
    ),
    pytest.param(
        _signals(live=True, returncode=0, pid=RECORDED_PID),
        ("completed", True, domain.ClassifyReason.LIVE_HANDLE_EXITED),
        id="live-handle-exited-ok",
    ),
    pytest.param(
        _signals(live=True, returncode=1, pid=RECORDED_PID),
        ("completed", False, domain.ClassifyReason.LIVE_HANDLE_EXITED),
        id="live-handle-exited-failed",
    ),
    pytest.param(
        _signals(report=True, pid=RECORDED_PID),
        ("completed", True, domain.ClassifyReason.REPORT_PRESENT),
        id="recovered-report-ok",
    ),
    pytest.param(
        _signals(report=False, pid=RECORDED_PID, alive=True),
        ("completed", False, domain.ClassifyReason.REPORT_PRESENT),
        id="recovered-report-failed",
    ),
    pytest.param(
        _signals(pid=RECORDED_PID, alive=True),
        ("running", None, domain.ClassifyReason.PID_ALIVE_NO_REPORT),
        id="recovered-pid-alive",
    ),
    pytest.param(
        _signals(pid=RECORDED_PID),
        ("unknown", False, domain.ClassifyReason.PID_DEAD_NO_REPORT),
        id="recovered-pid-dead",
    ),
    pytest.param(
        _signals(),
        ("unknown", False, domain.ClassifyReason.PID_UNKNOWN_NO_REPORT),
        id="recovered-pid-unknown",
    ),
]


def test_every_job_state_reason_is_reached_by_a_case():
    reasons = {case.values[1][2] for case in JOB_STATE_CASES}
    assert reasons == set(domain.ClassifyReason)


@pytest.mark.parametrize("signals, expected", JOB_STATE_CASES)
def test_classify_job_state(signals, expected):
    result = domain.classify_job_state(**signals)

    assert (result.status, result.ok, result.reason) == expected


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


SCENARIO_STATUS_FOR_BEHAVE = {
    domain.BehaveScenarioStatus.PASSED: "passed",
    domain.BehaveScenarioStatus.FAILED: "failed",
    domain.BehaveScenarioStatus.ERROR: "failed",
    domain.BehaveScenarioStatus.HOOK_ERROR: "failed",
    domain.BehaveScenarioStatus.CLEANUP_ERROR: "failed",
    domain.BehaveScenarioStatus.UNDEFINED: "failed",
    domain.BehaveScenarioStatus.SKIPPED: "skipped",
}


def test_every_behave_scenario_status_has_an_expectation():
    assert set(SCENARIO_STATUS_FOR_BEHAVE) == set(domain.BehaveScenarioStatus)


@pytest.mark.parametrize(
    "reported, expected", SCENARIO_STATUS_FOR_BEHAVE.items(), ids=str
)
def test_a_reported_scenario_status_wins_over_the_steps(reported, expected):
    scenario = {
        "status": reported.value,
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }

    assert domain.scenario_status_from_element(scenario) == expected


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


def test_machine_type_errors_list_every_allowed_machine_type():
    limits = {pool: 1 for pool in Pool}

    missing = domain.validate_machine_types([], limits)
    unknown = domain.validate_machine_types(["lxd-ctr"], limits)

    for message in (missing, unknown):
        assert message is not None
        assert "aws.pro-fips" in message
        assert "lxd-vm" in message
        assert "wsl" in message
