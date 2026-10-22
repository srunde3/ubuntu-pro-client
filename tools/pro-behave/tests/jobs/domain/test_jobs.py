"""Job rules: the behave command, exact names, job state, result filters."""

import itertools
import subprocess
import sys

import pytest

from pro_behave.jobs.domain.jobs import (
    UnitMatch,
    build_command,
    exact_name_pattern,
    interruption,
    job_matches_result_filters,
    job_state,
    match_unit,
    validate_machine_type,
)
from pro_behave.jobs.domain.pools import Pool
from pro_behave.jobs.messages import Interruption, JobExit, JobRecord, JobState


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


# The exits a job can have: none yet, one from tox ending, one from a kill.
EXITS = {
    "no-exit": None,
    "exited": JobExit(returncode=0, ok=True),
    "killed": JobExit(returncode=-15, killed=True),
}

# A job's state for every exit, "it has a launch record" and "its process
# holds the job lock". The job process writes the exit before letting go of
# the lock, so an exit decides even while the lock is still held; the launch
# takes the lock before it writes the record, so a held lock without a record
# is a launch in progress, still queued.
JOB_STATE = {
    ("no-exit", False, False): JobState.QUEUED,
    ("no-exit", False, True): JobState.QUEUED,
    ("no-exit", True, True): JobState.RUNNING,
    ("no-exit", True, False): JobState.INTERRUPTED,
    ("exited", False, False): JobState.FINISHED,
    ("exited", False, True): JobState.FINISHED,
    ("exited", True, True): JobState.FINISHED,
    ("exited", True, False): JobState.FINISHED,
    ("killed", False, False): JobState.INTERRUPTED,
    ("killed", False, True): JobState.INTERRUPTED,
    ("killed", True, True): JobState.INTERRUPTED,
    ("killed", True, False): JobState.INTERRUPTED,
}


def test_every_combination_has_an_expected_state():
    assert set(JOB_STATE) == set(
        itertools.product(EXITS, (False, True), (False, True))
    )


def test_every_state_is_reached():
    assert set(JOB_STATE.values()) == set(JobState)


@pytest.mark.parametrize(
    "exit, launched, lock_held, expected",
    [(e, la, h, want) for (e, la, h), want in JOB_STATE.items()],
    ids=str,
)
def test_job_state(exit, launched, lock_held, expected):
    state = job_state(exit=EXITS[exit], launched=launched, lock_held=lock_held)

    assert state == expected


# Why an interrupted job has no result: a kill leaves an exit saying so, and
# anything else that ends a job without an exit is its process dying.
INTERRUPTION = {
    "no-exit": Interruption.DIED,
    "killed": Interruption.KILLED,
}


def test_every_interruption_is_reached():
    assert set(INTERRUPTION.values()) == set(Interruption)


@pytest.mark.parametrize("exit, expected", INTERRUPTION.items(), ids=str)
def test_interruption(exit, expected):
    assert interruption(EXITS[exit]) == expected


def test_job_matches_result_filters_no_filters_matches_everything():
    assert job_matches_result_filters(
        JobRecord(),
        job_id="job1",
        job_ids=None,
        feature=None,
        scenario=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_job_ids():
    metadata = JobRecord()
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids={"job1", "job2"},
        feature=None,
        scenario=None,
        release=None,
        machine_type=None,
    )
    assert not job_matches_result_filters(
        metadata,
        job_id="job3",
        job_ids={"job1", "job2"},
        feature=None,
        scenario=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_feature():
    metadata = JobRecord(feature="features/cli/attach.feature")
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature="features/cli/attach.feature",
        scenario=None,
        release=None,
        machine_type=None,
    )
    assert not job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature="features/cli/other.feature",
        scenario=None,
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_scenario_name_substring():
    metadata = JobRecord(scenario="Attach invalid token")
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature=None,
        scenario="invalid",
        release=None,
        machine_type=None,
    )
    assert not job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature=None,
        scenario="expired",
        release=None,
        machine_type=None,
    )


def test_job_matches_result_filters_by_release_and_machine_type():
    metadata = JobRecord(release="jammy", machine_type="lxd-container")
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature=None,
        scenario=None,
        release="jammy",
        machine_type="lxd-container",
    )
    assert not job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature=None,
        scenario=None,
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
    assert "max_jobs.cloud" in message


def test_a_machine_type_in_an_enabled_pool_is_allowed():
    limits = {pool: 1 for pool in Pool}

    assert validate_machine_type("lxd-vm", limits) is None
