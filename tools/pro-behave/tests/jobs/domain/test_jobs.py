"""Job rules: job state, interruption and result filters."""

import itertools

import pytest

from pro_behave.jobs.domain.jobs import (
    interruption,
    job_matches_result_filters,
    job_state,
    validate_machine_type,
)
from pro_behave.jobs.domain.pools import Pool
from pro_behave.jobs.domain.results import killed_result
from pro_behave.jobs.messages import (
    Interruption,
    JobRecord,
    JobResult,
    JobState,
    Outcome,
)

# The results a job can have: none yet, one with an outcome, one from a kill.
RESULTS = {
    "no-result": None,
    "finished": JobResult(returncode=0, outcome=Outcome.PASSED),
    "killed": killed_result("T1", returncode=-15),
}

# A job's state for every result, "it has a launch record" and "its process
# holds the job lock". The job process writes the result before letting go
# of the lock, so a result decides even while the lock is still held; the
# launch
# takes the lock before it writes the record, so a held lock without a
# record is a launch in progress, still queued.
JOB_STATE = {
    ("no-result", False, False): JobState.QUEUED,
    ("no-result", False, True): JobState.QUEUED,
    ("no-result", True, True): JobState.RUNNING,
    ("no-result", True, False): JobState.INTERRUPTED,
    ("finished", False, False): JobState.FINISHED,
    ("finished", False, True): JobState.FINISHED,
    ("finished", True, True): JobState.FINISHED,
    ("finished", True, False): JobState.FINISHED,
    ("killed", False, False): JobState.INTERRUPTED,
    ("killed", False, True): JobState.INTERRUPTED,
    ("killed", True, True): JobState.INTERRUPTED,
    ("killed", True, False): JobState.INTERRUPTED,
}


def test_every_combination_has_an_expected_state():
    assert set(JOB_STATE) == set(
        itertools.product(RESULTS, (False, True), (False, True))
    )


def test_every_state_is_reached():
    assert set(JOB_STATE.values()) == set(JobState)


@pytest.mark.parametrize(
    "result, launched, lock_held, expected",
    [(r, la, h, want) for (r, la, h), want in JOB_STATE.items()],
    ids=str,
)
def test_job_state(result, launched, lock_held, expected):
    state = job_state(
        result=RESULTS[result], launched=launched, lock_held=lock_held
    )

    assert state == expected


# Why an interrupted job has no outcome: a kill leaves a result saying so,
# and anything else that ends a job without a result is its process dying.
INTERRUPTION = {
    "no-result": Interruption.DIED,
    "killed": Interruption.KILLED,
}


def test_every_interruption_is_reached():
    assert set(INTERRUPTION.values()) == set(Interruption)


@pytest.mark.parametrize("result, expected", INTERRUPTION.items(), ids=str)
def test_interruption(result, expected):
    assert interruption(RESULTS[result]) == expected


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


def test_job_matches_result_filters_by_exact_scenario_name():
    metadata = JobRecord(scenario="Attach invalid token")
    assert job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature=None,
        scenario="Attach invalid token",
        release=None,
        machine_type=None,
    )
    assert not job_matches_result_filters(
        metadata,
        job_id="job1",
        job_ids=None,
        feature=None,
        scenario="invalid",
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
