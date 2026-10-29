"""What an ended job established for its unit."""

import pytest

from pro_behave.campaign.domain.classify import (
    DIED,
    INTERRUPTIONS,
    JobStatus,
    classify_result,
)
from pro_behave.campaign.domain.events import (
    MAX_EVENT_ERROR_CHARS,
    MAX_EVENT_FAILURES,
)
from pro_behave.campaign.domain.records import (
    Failure,
    InstanceResult,
    InstanceStatus,
    InterruptedRecord,
    Outcome,
    Unit,
)

AT = "2026-09-12T12:00:00Z"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")

ROW = InstanceResult(
    id="features/a.feature:10",
    name="A -- @1.1 ubuntu release",
    status=InstanceStatus.FAILED,
)

# Why the job decided its outcome, as its result says.
JOB_REASON = "the command exited 2"


def finished(outcome, job_id="job1", reason="", failures=()):
    return JobStatus(
        state="finished",
        job_id=job_id,
        outcome=outcome,
        reason=reason,
        instances=(ROW,),
        failures=tuple(failures),
    )


def failure(n, message="it did not"):
    return Failure(
        instance=ROW.id,
        step="step {}".format(n),
        status="failed",
        error_message=message,
    )


# Each outcome a job can decide is the attempt's outcome.
CLASSIFY_CASES = [
    pytest.param(finished(o.value), o, id=o.value) for o in Outcome
]


def test_every_outcome_is_reached_by_a_case():
    assert {case.values[1] for case in CLASSIFY_CASES} == set(Outcome)


@pytest.mark.parametrize("status, outcome", CLASSIFY_CASES)
def test_the_attempt_takes_the_job_s_outcome(status, outcome):
    classified = classify_result(UNIT_A, status, AT)

    assert classified.outcome == outcome


def test_the_attempt_keeps_the_job_s_reason_and_instances():
    classified = classify_result(
        UNIT_A, finished("error", reason=JOB_REASON), AT
    )

    assert (classified.reason, classified.instances) == (JOB_REASON, (ROW,))


def test_the_attempt_keeps_the_first_failing_steps():
    failures = [failure(n) for n in range(MAX_EVENT_FAILURES + 2)]

    classified = classify_result(
        UNIT_A, finished("failed", failures=failures), AT
    )

    assert classified.failures == tuple(failures[:MAX_EVENT_FAILURES])


def test_a_long_failure_message_is_cut():
    long = failure(1, "x" * (MAX_EVENT_ERROR_CHARS + 1))

    classified = classify_result(
        UNIT_A, finished("failed", failures=[long]), AT
    )

    assert len(classified.failures[0].error_message) == MAX_EVENT_ERROR_CHARS


@pytest.mark.parametrize("outcome", ["bogus", None], ids=repr)
def test_a_finish_the_campaign_cannot_read_is_an_error_that_says_so(outcome):
    classified = classify_result(UNIT_A, finished(outcome), AT)

    assert classified.outcome == Outcome.ERROR
    assert classified.reason.startswith(
        "the campaign could not read the job's status"
    )


@pytest.mark.parametrize("reason", INTERRUPTIONS)
def test_an_interrupted_job_is_an_interrupted_attempt(reason):
    status = JobStatus(state="interrupted", job_id="job1", interruption=reason)

    closed = classify_result(UNIT_A, status, AT)

    assert closed == InterruptedRecord(
        unit=UNIT_A, job_id="job1", at=AT, reason=reason
    )


def test_an_interruption_with_no_known_reason_is_a_death():
    status = JobStatus(state="interrupted", job_id="job1")

    closed = classify_result(UNIT_A, status, AT)

    assert closed.reason == DIED


def test_a_classified_result_keeps_the_job_id_and_time():
    classified = classify_result(UNIT_A, finished("passed"), AT)

    assert (classified.job_id, classified.at) == ("job1", AT)
