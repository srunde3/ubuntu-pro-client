"""Job rules: the behave command, exact names, job state, result filters."""

import subprocess
import sys

import pytest

from behave_mcp.domain.jobs import (
    ClassifyReason,
    UnitMatch,
    build_command,
    classify_job_state,
    exact_name_pattern,
    job_matches_result_filters,
    match_unit,
    validate_machine_type,
)
from behave_mcp.domain.pools import Pool
from behave_mcp.messages import JobRecord


def test_build_command_escapes_scenario_name_for_behave_regex(tmp_path):
    command = build_command(
        "features/proxy_config.feature",
        "apt_http(s)_proxy still works",
        "bionic",
        "lxd-container",
        tmp_path / "report.json",
    )

    assert command[command.index("--name") + 1] == (
        r"^apt_http\(s\)_proxy\ still\ works(?: -- @.*)?$"
    )


def test_build_command_runs_one_release_on_one_machine_type(tmp_path):
    report = tmp_path / "report.json"

    command = build_command(
        "features/cli/attach.feature",
        "Attach command",
        "jammy",
        "lxd-vm",
        report,
    )

    assert command == [
        "tox",
        "-e",
        "behave",
        "--",
        "features/cli/attach.feature",
        "--name",
        exact_name_pattern("Attach command"),
        "-D",
        "releases=jammy",
        "-D",
        "machine_types=lxd-vm",
        "-f",
        "json",
        "-o",
        str(report),
        "-f",
        "plain",
    ]


SCENARIO = "Attach command"
OTHER_SCENARIO = "Detach command"
JAMMY_LXD = ("jammy", "lxd-container")
NOBLE_VM = ("noble", "lxd-vm")


UNIT_MATCH_CASES = [
    pytest.param(
        [(SCENARIO, {JAMMY_LXD})], UnitMatch.FOUND, id="one-scenario"
    ),
    pytest.param(
        [(SCENARIO, {JAMMY_LXD}), (SCENARIO, {NOBLE_VM})],
        UnitMatch.FOUND,
        id="a-shared-name-on-other-combos",
    ),
    pytest.param(
        [(OTHER_SCENARIO, {JAMMY_LXD})],
        UnitMatch.NO_SCENARIO,
        id="no-such-name",
    ),
    pytest.param(
        [(SCENARIO, {NOBLE_VM})],
        UnitMatch.NOT_ON_COMBO,
        id="not-on-this-combo",
    ),
    pytest.param(
        [(SCENARIO, {JAMMY_LXD}), (SCENARIO, {JAMMY_LXD, NOBLE_VM})],
        UnitMatch.AMBIGUOUS,
        id="two-scenarios-on-one-combo",
    ),
]


def test_every_unit_match_is_reached_by_a_case():
    assert {case.values[1] for case in UNIT_MATCH_CASES} == set(UnitMatch)


@pytest.mark.parametrize("scenarios, expected", UNIT_MATCH_CASES)
def test_match_unit(scenarios, expected):
    release, machine_type = JAMMY_LXD

    match = match_unit(scenarios, SCENARIO, release, machine_type)

    assert match == expected


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
    metadata = JobRecord(release="jammy", machine_type="lxd-container")
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


def test_an_unknown_machine_type_lists_every_allowed_one():
    limits = {pool: 1 for pool in Pool}

    message = validate_machine_type("lxd-ctr", limits)

    assert message is not None
    assert "aws.pro-fips" in message
    assert "lxd-vm" in message
    assert "wsl" in message


def test_a_machine_type_in_a_disabled_pool_names_the_setting():
    limits = {Pool.LXD: 1, Pool.CLOUD: 0, Pool.WSL: 1}

    message = validate_machine_type("aws.generic", limits)

    assert message is not None
    assert "MCP_MAX_CLOUD_JOBS" in message


def test_a_machine_type_in_an_enabled_pool_is_allowed():
    limits = {pool: 1 for pool in Pool}

    assert validate_machine_type("lxd-vm", limits) is None
