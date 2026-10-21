"""What a scheduler tick decides."""

import pytest

from behave_campaign.domain.records import (
    AttemptFinished,
    AttemptStarted,
    CampaignHeader,
    CampaignState,
    Cause,
    Intent,
    IntentRecord,
    PlanRecord,
    Unit,
)
from behave_campaign.domain.schedule import Lane, plan_tick
from behave_mcp.domain.pools import Pool

AT = "2026-09-12T12:00:00Z"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")


UNIT_C = Unit("features/c.feature", "C", "focal", "lxd-container")


UNIT_W1 = Unit("features/w.feature", "W", "jammy", "wsl")


UNIT_W2 = Unit("features/w.feature", "W", "noble", "wsl")


UNIT_CLOUD = Unit("features/d.feature", "D", "jammy", "aws.generic")


def lxd(lanes):
    return {Pool.LXD: lanes}


def plan(*units):
    return [CampaignHeader(at=AT), *(PlanRecord(unit=u, at=AT) for u in units)]


def attempt(unit, outcome, job_id="job1"):
    """One record standing for a try.

    A try still in flight is just its start; a finished one is just its
    finish, which reduce_units reads as a whole attempt -- the shape an
    out-of-band recording takes. Tests that need a real start/finish pair
    go through the runner or the service.
    """
    if outcome == "running":
        return AttemptStarted(
            unit=unit, job_id=job_id, install_from="proposed", at=AT
        )
    return AttemptFinished(unit=unit, job_id=job_id, outcome=outcome, at=AT)


def completed(
    passed=1, failed=0, skipped=0, unknown=0, ok=True, job_id="job1"
):
    scenarios = {"passed": passed, "failed": failed, "skipped": skipped}
    if unknown:
        scenarios["unknown"] = unknown
    return {
        "status": "completed",
        "ok": ok,
        "job_id": job_id,
        "summary": {"scenarios": scenarios, "features": {}},
    }


class TestPlanTick:
    def test_each_pool_fills_only_its_own_free_lanes(self):
        records = [
            *plan(UNIT_W1, UNIT_W2, UNIT_A, UNIT_B, UNIT_C, UNIT_CLOUD),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]

        tick = plan_tick(
            records=records,
            lanes=[],
            lane_limits={Pool.LXD: 2, Pool.CLOUD: 1, Pool.WSL: 1},
            at=AT,
        )

        assert sorted(unit.pool for unit in tick.start) == [
            Pool.CLOUD,
            Pool.LXD,
            Pool.LXD,
            Pool.WSL,
        ]

    def test_a_busy_wsl_lane_does_not_hold_back_lxd(self):
        records = [
            *plan(UNIT_W1, UNIT_W2, UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_W1, "running"),
        ]
        lanes = [Lane(unit=UNIT_W1, job_id="job1")]

        tick = plan_tick(
            records=records,
            lanes=lanes,
            lane_limits={Pool.LXD: 2, Pool.WSL: 1},
            at=AT,
        )

        assert set(tick.start) == {UNIT_A, UNIT_B}

    def test_a_pool_without_lanes_starts_nothing(self):
        records = [
            *plan(UNIT_W1, UNIT_A),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(4), at=AT)

        assert tick.start == (UNIT_A,)

    def test_a_created_campaign_starts_nothing(self):
        tick = plan_tick(
            records=plan(UNIT_A), lanes=[], lane_limits=lxd(4), at=AT
        )

        assert tick.idle
        assert tick.state == CampaignState.CREATED

    def test_a_running_campaign_fills_every_free_lane(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(2), at=AT)

        assert len(tick.start) == 2
        assert tick.state == CampaignState.RUNNING

    def test_it_never_opens_more_lanes_than_the_limit(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1")]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(2), at=AT
        )

        assert len(tick.start) == 1

    def test_a_full_window_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
        ]
        lanes = [
            Lane(unit=UNIT_A, job_id="job1"),
            Lane(unit=UNIT_B, job_id="job2"),
        ]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(2), at=AT
        )

        assert tick.start == ()

    def test_a_finished_lane_is_recorded(self):
        records = [
            *plan(UNIT_A),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(2), at=AT
        )

        assert [f.outcome for f in tick.record] == ["passed"]

    def test_a_freed_lane_is_refilled_in_the_same_tick(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(1), at=AT
        )

        assert [f.unit for f in tick.record] == [UNIT_A]
        assert tick.start == (UNIT_B,)

    def test_a_unit_in_flight_is_never_started_twice(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1")]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(4), at=AT
        )

        assert UNIT_A not in tick.start

    def test_a_failed_unit_is_not_retried_on_its_own(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "passed"),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(4), at=AT)

        assert tick.start == ()
        assert tick.state == CampaignState.COMPLETE

    def test_a_paused_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
            IntentRecord(Intent.HOLD, Cause.PAUSE, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(4), at=AT
        )

        assert [f.outcome for f in tick.record] == ["passed"]
        assert tick.start == ()
        assert tick.state == CampaignState.PAUSED

    def test_the_last_finished_lane_completes_the_campaign(self):
        records = [
            *plan(UNIT_A),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(4), at=AT
        )

        assert tick.state == CampaignState.COMPLETE
        assert tick.start == ()

    def test_an_unclassifiable_result_does_not_stop_scheduling(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [
            Lane(
                unit=UNIT_A,
                job_id="job1",
                result=completed(passed=1, unknown=1),
            )
        ]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(2), at=AT
        )

        assert tick.record[0].problem
        # UNIT_A was just recorded as error. It must not be re-queued: only
        # the untouched unit is started.
        assert tick.start == (UNIT_B,)


# Whether a tick opens lanes under each intent. Only run schedules.
OPENS_LANES = {
    Intent.NEVER_RUN: False,
    Intent.RUN: True,
    Intent.HOLD: False,
}


def test_every_intent_has_an_expectation():
    assert set(OPENS_LANES) == set(Intent)


@pytest.mark.parametrize("wanted, opens", OPENS_LANES.items(), ids=str)
def test_only_intent_run_opens_lanes(wanted, opens):
    intent_records = (
        []
        if wanted == Intent.NEVER_RUN
        else [IntentRecord(wanted, Cause.RUN, AT)]
    )
    records = [*plan(UNIT_A, UNIT_B), *intent_records]

    tick = plan_tick(records=records, lanes=[], lane_limits=lxd(8), at=AT)

    assert bool(tick.start) is opens


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

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(8), at=AT)

        assert tick.start == ()

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_problem_units_do_not_crowd_out_untouched_ones(self, state):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, state),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(1), at=AT)

        assert tick.start == (UNIT_B,)

    def test_a_lane_that_errors_frees_capacity_for_untouched_work(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed(passed=0))]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(1), at=AT
        )

        assert tick.record[0].outcome == "error"
        assert tick.start == (UNIT_B,)
