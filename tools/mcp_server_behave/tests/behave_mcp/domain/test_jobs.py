"""Job rules: the behave command, exact names, job state, result filters."""

import subprocess
import sys

import pytest

from behave_mcp.domain.jobs import (
    ClassifyReason,
    build_command,
    classify_job_state,
    exact_name_pattern,
    job_matches_result_filters,
    validate_machine_types,
)
from behave_mcp.domain.pools import Pool
from behave_mcp.messages import JobRecord


def test_build_command_escapes_scenario_name_for_behave_regex(tmp_path):
    command = build_command(
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
            exact_name_pattern("Attach command"),
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
        ("running", None, ClassifyReason.LIVE_HANDLE_RUNNING),
        id="live-handle-running",
    ),
    pytest.param(
        _signals(live=True, returncode=0, pid=RECORDED_PID),
        ("completed", True, ClassifyReason.LIVE_HANDLE_EXITED),
        id="live-handle-exited-ok",
    ),
    pytest.param(
        _signals(live=True, returncode=1, pid=RECORDED_PID),
        ("completed", False, ClassifyReason.LIVE_HANDLE_EXITED),
        id="live-handle-exited-failed",
    ),
    pytest.param(
        _signals(report=True, pid=RECORDED_PID),
        ("completed", True, ClassifyReason.REPORT_PRESENT),
        id="recovered-report-ok",
    ),
    pytest.param(
        _signals(report=False, pid=RECORDED_PID, alive=True),
        ("completed", False, ClassifyReason.REPORT_PRESENT),
        id="recovered-report-failed",
    ),
    pytest.param(
        _signals(pid=RECORDED_PID, alive=True),
        ("running", None, ClassifyReason.PID_ALIVE_NO_REPORT),
        id="recovered-pid-alive",
    ),
    pytest.param(
        _signals(pid=RECORDED_PID),
        ("unknown", False, ClassifyReason.PID_DEAD_NO_REPORT),
        id="recovered-pid-dead",
    ),
    pytest.param(
        _signals(),
        ("unknown", False, ClassifyReason.PID_UNKNOWN_NO_REPORT),
        id="recovered-pid-unknown",
    ),
]


def test_every_job_state_reason_is_reached_by_a_case():
    reasons = {case.values[1][2] for case in JOB_STATE_CASES}
    assert reasons == set(ClassifyReason)


@pytest.mark.parametrize("signals, expected", JOB_STATE_CASES)
def test_classify_job_state(signals, expected):
    result = classify_job_state(**signals)

    assert (result.status, result.ok, result.reason) == expected


def test_job_matches_result_filters_no_filters_matches_everything():
    assert job_matches_result_filters(
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
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids={"job1", "job2"},
        feature_file=None,
        scenario_name=None,
        release=None,
        machine_type=None,
    )
    assert not job_matches_result_filters(
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
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file="features/cli/attach.feature",
        scenario_name=None,
        release=None,
        machine_type=None,
    )
    assert not job_matches_result_filters(
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
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name="invalid",
        release=None,
        machine_type=None,
    )
    assert not job_matches_result_filters(
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
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release="jammy",
        machine_type="lxd-container",
    )
    assert not job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature_file=None,
        scenario_name=None,
        release="resolute",
        machine_type=None,
    )


def test_machine_type_errors_list_every_allowed_machine_type():
    limits = {pool: 1 for pool in Pool}

    missing = validate_machine_types([], limits)
    unknown = validate_machine_types(["lxd-ctr"], limits)

    for message in (missing, unknown):
        assert message is not None
        assert "aws.pro-fips" in message
        assert "lxd-vm" in message
        assert "wsl" in message
