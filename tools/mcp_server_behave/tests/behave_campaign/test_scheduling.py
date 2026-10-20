"""The pure scheduling rules: lifecycle, classification, and plan_tick.

No thread, no store, no jobs -- everything here is a function of records and
lane reports, which is what makes the scheduler exhaustively testable.
"""

import pytest

from behave_campaign.domain import (
    AttemptFinished,
    AttemptStarted,
    CampaignHeader,
    Lane,
    Lifecycle,
    LifecycleRecord,
    Outcome,
    PlanRecord,
    RetryRecord,
    Unit,
    UnitState,
    UnitStatus,
    classify_result,
    detect_signals,
    elapsed_seconds,
    is_schedulable,
    last_state_reason,
    lifecycle,
    outstanding,
    plan_tick,
)
from behave_mcp.pools import Pool

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


class TestLifecycle:
    def test_a_campaign_with_no_state_record_is_created(self):
        assert lifecycle(plan(UNIT_A)) == Lifecycle.CREATED

    def test_the_latest_state_record_wins(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(state=Lifecycle.RUNNING, at=AT),
            LifecycleRecord(state=Lifecycle.PAUSED, at=AT),
        ]

        assert lifecycle(records) == Lifecycle.PAUSED

    def test_a_resumed_campaign_is_running_again(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(state=Lifecycle.RUNNING, at=AT),
            LifecycleRecord(state=Lifecycle.PAUSED, at=AT),
            LifecycleRecord(state=Lifecycle.RUNNING, at=AT),
        ]

        assert lifecycle(records) == Lifecycle.RUNNING

    def test_running_with_work_left_stays_running(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
        ]

        assert lifecycle(records) == Lifecycle.RUNNING

    def test_running_with_a_job_in_flight_stays_running(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "running"),
        ]

        assert lifecycle(records) == Lifecycle.RUNNING

    def test_running_with_nothing_left_stays_running_until_marked(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "passed"),
            attempt(UNIT_B, "passed"),
        ]

        assert lifecycle(records) == Lifecycle.RUNNING
        assert outstanding(records) == []

    def test_a_written_complete_is_complete(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "passed"),
            LifecycleRecord(Lifecycle.COMPLETE, AT),
        ]

        assert lifecycle(records) == Lifecycle.COMPLETE

    def test_problem_units_do_not_count_as_remaining_work(self):
        # Nothing retries a failure on its own.
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "skipped"),
        ]

        assert outstanding(records) == []

    def test_unattempted_running_and_retried_units_are_outstanding(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            attempt(UNIT_A, "running"),
            attempt(UNIT_B, "failed"),
            RetryRecord(unit=UNIT_B, at=AT),
        ]

        assert [s.unit for s in outstanding(records)] == [
            UNIT_A,
            UNIT_B,
            UNIT_C,
        ]

    def test_a_paused_campaign_with_nothing_left_stays_paused(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "passed"),
            LifecycleRecord(Lifecycle.PAUSED, AT),
        ]

        assert lifecycle(records) == Lifecycle.PAUSED

    def test_a_cancelled_campaign_stays_cancelled(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            LifecycleRecord(Lifecycle.CANCELLED, AT),
        ]

        assert lifecycle(records) == Lifecycle.CANCELLED

    def test_the_reason_of_the_latest_transition_is_reported(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            LifecycleRecord(Lifecycle.PAUSED, AT, reason="server_restart"),
        ]

        assert last_state_reason(records) == "server_restart"

    def test_no_transition_has_no_reason(self):
        assert last_state_reason(plan(UNIT_A)) == ""


CLASSIFY_CASES = [
    pytest.param(completed(passed=2), Outcome.PASSED, False, id="clean-pass"),
    # A skipped Examples row next to a passing one: that run passed.
    pytest.param(
        completed(passed=1, skipped=1),
        Outcome.PASSED,
        False,
        id="pass-and-skip",
    ),
    pytest.param(
        completed(passed=1, failed=1), Outcome.FAILED, False, id="any-failure"
    ),
    pytest.param(
        completed(passed=0, skipped=2), Outcome.SKIPPED, False, id="only-skips"
    ),
    pytest.param(
        {"status": "completed", "ok": False, "job_id": "j", "summary": None},
        Outcome.ERROR,
        False,
        id="no-report",
    ),
    # The CLI raises on these. A scheduler has nobody to raise at, so it
    # records an error with the reason and keeps going.
    pytest.param(
        completed(passed=1, unknown=1),
        Outcome.ERROR,
        True,
        id="unknown-scenario-status",
    ),
    pytest.param(
        completed(passed=1, ok=False),
        Outcome.ERROR,
        True,
        id="pass-contradicted-by-ok-false",
    ),
    pytest.param("not a payload", Outcome.ERROR, True, id="junk"),
]


def test_every_outcome_is_reached_by_a_case():
    assert {case.values[1] for case in CLASSIFY_CASES} == set(Outcome)


@pytest.mark.parametrize("result, outcome, flagged", CLASSIFY_CASES)
def test_classify_result(result, outcome, flagged):
    classified = classify_result(UNIT_A, result, AT)

    assert classified.finished.outcome == outcome
    assert bool(classified.problem) is flagged


def test_a_classified_result_keeps_the_job_id_and_time():
    classified = classify_result(UNIT_A, completed(passed=2), AT)

    assert classified.finished.job_id == "job1"
    assert classified.finished.at == AT


def test_an_unclassifiable_result_keeps_the_job_id_and_says_why():
    classified = classify_result(
        UNIT_A, completed(passed=1, unknown=1, job_id="job-xyz"), AT
    )

    assert classified.finished.job_id == "job-xyz"
    assert "classify" in classified.problem


def _status(state, retry_pending):
    return UnitStatus(
        unit=UNIT_A,
        state=state,
        job_id=None,
        attempts=(),
        retry_pending=retry_pending,
    )


# A unit is picked up when it is untouched, or when someone asked for a retry
# since its last attempt. A unit in flight is never picked up twice.
SCHEDULABLE = {
    (UnitState.UNATTEMPTED, False): True,
    (UnitState.UNATTEMPTED, True): True,
    (UnitState.RUNNING, False): False,
    (UnitState.RUNNING, True): False,
    (UnitState.PASSED, False): False,
    (UnitState.PASSED, True): True,
    (UnitState.FAILED, False): False,
    (UnitState.FAILED, True): True,
    (UnitState.SKIPPED, False): False,
    (UnitState.SKIPPED, True): True,
    (UnitState.ERROR, False): False,
    (UnitState.ERROR, True): True,
}


def test_every_state_and_retry_flag_has_an_expectation():
    assert set(SCHEDULABLE) == {
        (state, retry) for state in UnitState for retry in (False, True)
    }


@pytest.mark.parametrize(
    "state, retry_pending, expected",
    [(state, retry, want) for (state, retry), want in SCHEDULABLE.items()],
    ids=str,
)
def test_schedulable_by_state_and_retry(state, retry_pending, expected):
    assert is_schedulable(_status(state, retry_pending)) is expected


class TestPlanTick:
    def test_each_pool_fills_only_its_own_free_lanes(self):
        records = [
            *plan(UNIT_W1, UNIT_W2, UNIT_A, UNIT_B, UNIT_C, UNIT_CLOUD),
            LifecycleRecord(Lifecycle.RUNNING, AT),
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
            LifecycleRecord(Lifecycle.RUNNING, AT),
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
            LifecycleRecord(Lifecycle.RUNNING, AT),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(4), at=AT)

        assert tick.start == (UNIT_A,)

    def test_a_created_campaign_starts_nothing(self):
        tick = plan_tick(
            records=plan(UNIT_A), lanes=[], lane_limits=lxd(4), at=AT
        )

        assert tick.idle
        assert tick.lifecycle == Lifecycle.CREATED

    def test_a_running_campaign_fills_every_free_lane(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            LifecycleRecord(Lifecycle.RUNNING, AT),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(2), at=AT)

        assert len(tick.start) == 2
        assert tick.lifecycle == Lifecycle.RUNNING

    def test_it_never_opens_more_lanes_than_the_limit(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            LifecycleRecord(Lifecycle.RUNNING, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1")]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(2), at=AT
        )

        assert len(tick.start) == 1

    def test_a_full_window_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
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
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(2), at=AT
        )

        assert [c.finished.outcome for c in tick.record] == ["passed"]

    def test_a_freed_lane_is_refilled_in_the_same_tick(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(1), at=AT
        )

        assert [c.finished.unit for c in tick.record] == [UNIT_A]
        assert tick.start == (UNIT_B,)

    def test_a_unit_in_flight_is_never_started_twice(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
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
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "passed"),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(4), at=AT)

        assert tick.start == ()
        assert tick.lifecycle == Lifecycle.COMPLETE
        assert tick.complete

    def test_a_paused_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "running"),
            LifecycleRecord(Lifecycle.PAUSED, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(4), at=AT
        )

        assert [c.finished.outcome for c in tick.record] == ["passed"]
        assert tick.start == ()
        assert tick.lifecycle == Lifecycle.PAUSED

    def test_a_cancelled_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "running"),
            LifecycleRecord(Lifecycle.CANCELLED, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(4), at=AT
        )

        assert len(tick.record) == 1
        assert tick.start == ()
        assert tick.lifecycle == Lifecycle.CANCELLED

    def test_the_last_finished_lane_completes_the_campaign(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(4), at=AT
        )

        assert tick.lifecycle == Lifecycle.COMPLETE
        assert tick.start == ()

    def test_an_unclassifiable_result_does_not_stop_scheduling(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
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


@pytest.mark.parametrize("state", [Lifecycle.PAUSED, Lifecycle.CANCELLED])
def test_no_lane_is_ever_opened_outside_running(state):
    records = [
        *plan(UNIT_A, UNIT_B),
        LifecycleRecord(Lifecycle.RUNNING, AT),
        LifecycleRecord(state, AT),
    ]

    assert (
        plan_tick(records=records, lanes=[], lane_limits=lxd(8), at=AT).start
        == ()
    )


class TestSchedulerNeverRetries:
    """A non-passing unit is only ever re-run when someone asks for it."""

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_a_problem_unit_is_never_started_automatically(self, state):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, state),
            attempt(UNIT_B, "passed"),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(8), at=AT)

        assert tick.start == ()

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_problem_units_do_not_crowd_out_untouched_ones(self, state):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, state),
        ]

        tick = plan_tick(records=records, lanes=[], lane_limits=lxd(1), at=AT)

        assert tick.start == (UNIT_B,)

    def test_a_lane_that_errors_frees_capacity_for_untouched_work(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(Lifecycle.RUNNING, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed(passed=0))]

        tick = plan_tick(
            records=records, lanes=lanes, lane_limits=lxd(1), at=AT
        )

        assert tick.record[0].finished.outcome == "error"
        assert tick.start == (UNIT_B,)


class TestDetectSignals:
    """Derived observations for a watcher to judge. They act on nothing."""

    def status(self, unit, state, at=AT):
        from behave_campaign.domain import reduce_units

        records = [*plan(unit)]
        if state != "unattempted":
            records.append(
                AttemptFinished(unit=unit, job_id="job", outcome=state, at=at)
            )
        return reduce_units(records)[0]

    def failing_across(self, releases):
        return [
            self.status(
                Unit("features/a.feature", "A", release, "lxd-vm"), "failed"
            )
            for release in releases
        ]

    def test_one_scenario_failing_everywhere_is_reported(self):
        statuses = self.failing_across(["focal", "jammy", "noble"])

        signals = detect_signals(statuses=statuses, lanes=[], at=AT)

        assert [s.kind for s in signals] == [
            "anomaly.repeated_scenario_failure"
        ]
        assert signals[0].data["scenario"] == "A"
        assert signals[0].data["releases"] == ["focal", "jammy", "noble"]

    def test_a_failure_on_too_few_releases_is_not_reported(self):
        statuses = self.failing_across(["focal", "jammy"])

        assert detect_signals(statuses=statuses, lanes=[], at=AT) == []

    def test_a_run_of_skips_is_reported(self):
        statuses = [
            self.status(
                Unit("features/a.feature", "A", "jammy", "lxd-vm"), "skipped"
            )
        ] * 5

        signals = detect_signals(statuses=statuses, lanes=[], at=AT)

        assert [s.kind for s in signals] == ["anomaly.repeated_skips"]
        assert signals[0].data["skipped"] == 5

    def test_a_few_skips_are_not_reported(self):
        statuses = [
            self.status(
                Unit("features/a.feature", "A", "jammy", "lxd-vm"), "skipped"
            )
        ] * 4

        assert detect_signals(statuses=statuses, lanes=[], at=AT) == []

    def test_a_lane_open_too_long_is_reported(self):
        lane = Lane(
            unit=UNIT_A,
            job_id="job1",
            opened_at="2026-09-12T12:00:00Z",
        )

        signals = detect_signals(
            statuses=[],
            lanes=[lane],
            at="2026-09-12T14:00:00Z",
            overdue_seconds=3600,
        )

        assert [s.kind for s in signals] == ["lane.overdue"]
        assert signals[0].data["elapsed_seconds"] == 7200
        assert signals[0].key == "job1"

    def test_a_young_lane_is_not_reported(self):
        lane = Lane(
            unit=UNIT_A, job_id="job1", opened_at="2026-09-12T12:00:00Z"
        )

        signals = detect_signals(
            statuses=[],
            lanes=[lane],
            at="2026-09-12T12:10:00Z",
            overdue_seconds=3600,
        )

        assert signals == []

    def test_a_finished_lane_is_never_overdue(self):
        lane = Lane(
            unit=UNIT_A,
            job_id="job1",
            result=completed(),
            opened_at="2026-09-12T12:00:00Z",
        )

        signals = detect_signals(
            statuses=[],
            lanes=[lane],
            at="2026-09-12T23:00:00Z",
            overdue_seconds=3600,
        )

        assert signals == []

    def test_an_unreadable_timestamp_is_ignored_rather_than_raised(self):
        lane = Lane(unit=UNIT_A, job_id="job1", opened_at="whenever")

        assert detect_signals(statuses=[], lanes=[lane], at=AT) == []


class TestElapsedSeconds:
    def test_it_measures_between_campaign_timestamps(self):
        assert (
            elapsed_seconds("2026-09-12T12:00:00Z", "2026-09-12T12:01:30Z")
            == 90
        )

    @pytest.mark.parametrize(
        "since,now",
        [("nonsense", AT), (AT, "nonsense"), (None, AT)],
    )
    def test_anything_unreadable_is_none(self, since, now):
        assert elapsed_seconds(since, now) is None
