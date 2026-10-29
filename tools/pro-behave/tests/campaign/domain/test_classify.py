"""What a finished job established for its unit."""

import pytest

from pro_behave.campaign.domain.classify import (
    DIED,
    INTERRUPTIONS,
    classify_result,
    finished_from_mcp,
)
from pro_behave.campaign.domain.records import (
    CampaignError,
    InterruptedRecord,
    Outcome,
    Unit,
)

AT = "2026-09-12T12:00:00Z"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")

# Why the job decided its outcome, as its result says.
JOB_REASON = "the command exited 2"


def finished(outcome, job_id="job1", reason=""):
    return {
        "state": "finished",
        "job_id": job_id,
        "outcome": outcome,
        "reason": reason,
        "failures": [],
    }


# Each outcome a job can decide is the attempt's outcome.
CLASSIFY_CASES = [
    pytest.param(finished(o.value), o, id=o.value) for o in Outcome
]


def test_every_outcome_is_reached_by_a_case():
    assert {case.values[1] for case in CLASSIFY_CASES} == set(Outcome)


@pytest.mark.parametrize("result, outcome", CLASSIFY_CASES)
def test_the_attempt_takes_the_job_s_outcome(result, outcome):
    classified = classify_result(UNIT_A, result, AT)

    assert classified.outcome == outcome


def test_the_attempt_keeps_the_job_s_reason():
    classified = classify_result(
        UNIT_A, finished("error", reason=JOB_REASON), AT
    )

    assert classified.reason == JOB_REASON


# A scheduler has nobody to raise at, so a result it cannot read is
# recorded as an error, with the reason.
UNREADABLE = [
    pytest.param(finished("bogus"), id="unknown-outcome"),
    pytest.param(finished(None), id="no-outcome"),
    pytest.param("not a payload", id="junk"),
]


@pytest.mark.parametrize("result", UNREADABLE)
def test_an_unreadable_result_is_an_error_that_says_why(result):
    classified = classify_result(UNIT_A, result, AT)

    assert classified.outcome == Outcome.ERROR
    assert classified.reason


@pytest.mark.parametrize("reason", INTERRUPTIONS)
def test_an_interrupted_job_is_an_interrupted_attempt(reason):
    result = {"state": "interrupted", "interruption": reason, "job_id": "job1"}

    closed = classify_result(UNIT_A, result, AT)

    assert closed == InterruptedRecord(
        unit=UNIT_A, job_id="job1", at=AT, reason=reason
    )


def test_an_interruption_with_no_known_reason_is_a_death():
    result = {"state": "interrupted", "job_id": "job1"}

    closed = classify_result(UNIT_A, result, AT)

    assert closed.reason == DIED


def test_a_classified_result_keeps_the_job_id_and_time():
    classified = classify_result(UNIT_A, finished("passed"), AT)

    assert classified.job_id == "job1"
    assert classified.at == AT


def test_an_unreadable_result_keeps_the_job_id():
    classified = classify_result(
        UNIT_A, finished("bogus", job_id="job-xyz"), AT
    )

    assert classified.job_id == "job-xyz"
    assert "no known outcome" in classified.reason


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_job_still_in_flight_has_no_outcome_to_record(status):
    # The scheduler owns an unfinished job: it wrote the start itself and
    # will write the finish. Recording one here would invent a result.
    with pytest.raises(CampaignError) as error:
        finished_from_mcp(UNIT_A, {"state": status, "job_id": "job-1"})

    assert "only a finished job" in str(error.value)
