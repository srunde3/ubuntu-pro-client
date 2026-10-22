"""What one reconcile decides for a campaign."""

import pytest

from pro_behave.campaign.domain.records import (
    CampaignRecord,
    CampaignState,
    Cause,
    FinishedRecord,
    Intent,
    IntentRecord,
    PlanRecord,
    StartedRecord,
    Unit,
)
from pro_behave.campaign.domain.schedule import InFlightJob, plan_campaign
from pro_behave.jobs.domain.pools import Pool

AT = "2026-09-12T12:00:00Z"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")


UNIT_C = Unit("features/c.feature", "C", "focal", "lxd-container")


UNIT_W1 = Unit("features/w.feature", "W", "jammy", "wsl")


UNIT_W2 = Unit("features/w.feature", "W", "noble", "wsl")


UNIT_CLOUD = Unit("features/d.feature", "D", "jammy", "aws.generic")


def lxd(max_jobs):
    return {Pool.LXD: max_jobs}


def plan(*units):
    return [CampaignRecord(at=AT), *(PlanRecord(unit=u, at=AT) for u in units)]


def attempt(unit, outcome, job_id="job1"):
    """One record standing for a try.

    A try still in flight is just its start; a finished one is just its
    finish, which reduce_units reads as a whole attempt -- the shape an
    out-of-band recording takes. Tests that need a real start/finish pair
    go through the runner or the service.
    """
    if outcome == "running":
        return StartedRecord(
            unit=unit, job_id=job_id, install_from="proposed", at=AT
        )
    return FinishedRecord(unit=unit, job_id=job_id, outcome=outcome, at=AT)


def completed(
    passed=1, failed=0, skipped=0, unknown=0, ok=True, job_id="job1"
):
    scenarios = {"passed": passed, "failed": failed, "skipped": skipped}
    if unknown:
        scenarios["unknown"] = unknown
    return {
        "state": "finished",
        "ok": ok,
        "job_id": job_id,
        "summary": {"scenarios": scenarios, "features": {}},
    }


class TestPlanTick:
    def test_each_pool_fills_only_its_own_room(self):
        records = [
            *plan(UNIT_W1, UNIT_W2, UNIT_A, UNIT_B, UNIT_C, UNIT_CLOUD),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]

        decided = plan_campaign(
            records=records,
            jobs=[],
            max_jobs={Pool.LXD: 2, Pool.CLOUD: 1, Pool.WSL: 1},
            at=AT,
        )

        assert sorted(unit.pool for unit in decided.queue) == [
            Pool.CLOUD,
            Pool.LXD,
            Pool.LXD,
            Pool.WSL,
        ]

    def test_a_busy_wsl_job_does_not_hold_back_lxd(self):
        records = [
            *plan(UNIT_W1, UNIT_W2, UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_W1, "running"),
        ]
        jobs = [InFlightJob(unit=UNIT_W1, job_id="job1")]

        decided = plan_campaign(
            records=records,
            jobs=jobs,
            max_jobs={Pool.LXD: 2, Pool.WSL: 1},
            at=AT,
        )

        assert set(decided.queue) == {UNIT_A, UNIT_B}

    def test_a_pool_without_max_jobs_queues_nothing(self):
        records = [
            *plan(UNIT_W1, UNIT_A),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]

        decided = plan_campaign(
            records=records, jobs=[], max_jobs=lxd(4), at=AT
        )

        assert decided.queue == (UNIT_A,)

    def test_a_created_campaign_starts_nothing(self):
        decided = plan_campaign(
            records=plan(UNIT_A), jobs=[], max_jobs=lxd(4), at=AT
        )

        assert not decided.record and not decided.queue
        assert decided.state == CampaignState.CREATED

    def test_a_running_campaign_queues_up_to_its_max_jobs(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]

        decided = plan_campaign(
            records=records, jobs=[], max_jobs=lxd(2), at=AT
        )

        assert len(decided.queue) == 2
        assert decided.state == CampaignState.RUNNING

    def test_it_never_queues_more_than_max_jobs(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]
        jobs = [InFlightJob(unit=UNIT_A, job_id="job1")]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(2), at=AT
        )

        assert len(decided.queue) == 1

    def test_a_full_window_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]
        jobs = [
            InFlightJob(unit=UNIT_A, job_id="job1"),
            InFlightJob(unit=UNIT_B, job_id="job2"),
        ]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(2), at=AT
        )

        assert decided.queue == ()

    def test_an_ended_job_is_recorded(self):
        records = [
            *plan(UNIT_A),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        jobs = [InFlightJob(unit=UNIT_A, job_id="job1", result=completed())]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(2), at=AT
        )

        assert [f.outcome for f in decided.record] == ["passed"]

    def test_a_unit_freed_by_an_ended_job_is_replaced_at_once(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        jobs = [InFlightJob(unit=UNIT_A, job_id="job1", result=completed())]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(1), at=AT
        )

        assert [f.unit for f in decided.record] == [UNIT_A]
        assert decided.queue == (UNIT_B,)

    def test_a_unit_in_flight_is_never_started_twice(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        jobs = [InFlightJob(unit=UNIT_A, job_id="job1")]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(4), at=AT
        )

        assert UNIT_A not in decided.queue

    def test_a_failed_unit_is_not_retried_on_its_own(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "passed"),
        ]

        decided = plan_campaign(
            records=records, jobs=[], max_jobs=lxd(4), at=AT
        )

        assert decided.queue == ()
        assert decided.state == CampaignState.COMPLETE

    def test_a_paused_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
            IntentRecord(Intent.HOLD, Cause.PAUSE, AT),
        ]
        jobs = [InFlightJob(unit=UNIT_A, job_id="job1", result=completed())]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(4), at=AT
        )

        assert [f.outcome for f in decided.record] == ["passed"]
        assert decided.queue == ()
        assert decided.state == CampaignState.PAUSED

    def test_the_last_ended_job_completes_the_campaign(self):
        records = [
            *plan(UNIT_A),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        jobs = [InFlightJob(unit=UNIT_A, job_id="job1", result=completed())]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(4), at=AT
        )

        assert decided.state == CampaignState.COMPLETE
        assert decided.queue == ()

    def test_an_unclassifiable_result_does_not_stop_scheduling(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        jobs = [
            InFlightJob(
                unit=UNIT_A,
                job_id="job1",
                result=completed(passed=1, unknown=1),
            )
        ]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(2), at=AT
        )

        assert decided.record[0].reason
        # UNIT_A was just recorded as error. It must not be re-queued: only
        # the untouched unit is started.
        assert decided.queue == (UNIT_B,)


# Whether a reconcile queues under each intent. Only run does.
OPENS_LANES = {
    Intent.NEVER_RUN: False,
    Intent.RUN: True,
    Intent.HOLD: False,
}


def test_every_intent_has_an_expectation():
    assert set(OPENS_LANES) == set(Intent)


@pytest.mark.parametrize("wanted, opens", OPENS_LANES.items(), ids=str)
def test_only_intent_run_queues(wanted, opens):
    intent_records = (
        []
        if wanted == Intent.NEVER_RUN
        else [IntentRecord(wanted, Cause.RUN, AT)]
    )
    records = [*plan(UNIT_A, UNIT_B), *intent_records]

    decided = plan_campaign(records=records, jobs=[], max_jobs=lxd(8), at=AT)

    assert bool(decided.queue) is opens


class TestSchedulerNeverRetries:
    """A non-passing unit is only ever re-run when someone asks for it."""

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_a_problem_unit_is_never_started_automatically(self, state):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, state),
            attempt(UNIT_B, "passed"),
        ]

        decided = plan_campaign(
            records=records, jobs=[], max_jobs=lxd(8), at=AT
        )

        assert decided.queue == ()

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_problem_units_do_not_crowd_out_untouched_ones(self, state):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, state),
        ]

        decided = plan_campaign(
            records=records, jobs=[], max_jobs=lxd(1), at=AT
        )

        assert decided.queue == (UNIT_B,)

    def test_a_job_that_errors_frees_room_for_untouched_work(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        jobs = [
            InFlightJob(unit=UNIT_A, job_id="job1", result=completed(passed=0))
        ]

        decided = plan_campaign(
            records=records, jobs=jobs, max_jobs=lxd(1), at=AT
        )

        assert decided.record[0].outcome == "error"
        assert decided.queue == (UNIT_B,)
