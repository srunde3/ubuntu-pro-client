"""The pure scheduling rules: lifecycle, classification, and plan_tick.

No thread, no store, no jobs -- everything here is a function of records and
lane reports, which is what makes the scheduler exhaustively testable.
"""

import pytest

from behave_campaign.domain import (
    CANCELLED,
    COMPLETE,
    CREATED,
    PAUSED,
    RUNNING_STATE,
    AttemptFinished,
    AttemptStarted,
    CampaignHeader,
    Lane,
    LifecycleRecord,
    PlanRecord,
    Unit,
    classify_result,
    detect_signals,
    elapsed_seconds,
    last_state_reason,
    lifecycle,
    plan_tick,
)

AT = "2026-09-12T12:00:00Z"

UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")
UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")
UNIT_C = Unit("features/c.feature", "C", "focal", "lxd-container")


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
        assert lifecycle(plan(UNIT_A)) == CREATED

    def test_the_latest_state_record_wins(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(state=RUNNING_STATE, at=AT),
            LifecycleRecord(state=PAUSED, at=AT),
        ]

        assert lifecycle(records) == PAUSED

    def test_a_resumed_campaign_is_running_again(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(state=RUNNING_STATE, at=AT),
            LifecycleRecord(state=PAUSED, at=AT),
            LifecycleRecord(state=RUNNING_STATE, at=AT),
        ]

        assert lifecycle(records) == RUNNING_STATE

    def test_running_with_work_left_stays_running(self):
        records = [*plan(UNIT_A, UNIT_B), LifecycleRecord(RUNNING_STATE, AT)]

        assert lifecycle(records) == RUNNING_STATE

    def test_running_with_a_job_in_flight_stays_running(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]

        assert lifecycle(records) == RUNNING_STATE

    def test_running_with_nothing_left_is_complete(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "passed"),
            attempt(UNIT_B, "passed"),
        ]

        assert lifecycle(records) == COMPLETE

    def test_problem_units_do_not_count_as_remaining_work(self):
        # Nothing retries a failure on its own, so a campaign whose units
        # all finished is complete even when some of them failed.
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "skipped"),
        ]

        assert lifecycle(records) == COMPLETE

    def test_a_paused_campaign_with_nothing_left_stays_paused(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "passed"),
            LifecycleRecord(PAUSED, AT),
        ]

        assert lifecycle(records) == PAUSED

    def test_a_cancelled_campaign_stays_cancelled(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(RUNNING_STATE, AT),
            LifecycleRecord(CANCELLED, AT),
        ]

        assert lifecycle(records) == CANCELLED

    def test_the_reason_of_the_latest_transition_is_reported(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(RUNNING_STATE, AT),
            LifecycleRecord(PAUSED, AT, reason="server_restart"),
        ]

        assert last_state_reason(records) == "server_restart"

    def test_no_transition_has_no_reason(self):
        assert last_state_reason(plan(UNIT_A)) == ""


class TestClassifyResult:
    def test_a_clean_pass_is_recorded_as_passed(self):
        result = classify_result(UNIT_A, completed(passed=2), AT)

        assert result.finished.outcome == "passed"
        assert result.finished.job_id == "job1"
        assert result.finished.at == AT
        assert not result.problem

    def test_any_failure_is_recorded_as_failed(self):
        result = classify_result(UNIT_A, completed(passed=1, failed=1), AT)

        assert result.finished.outcome == "failed"
        assert not result.problem

    def test_a_skip_only_run_is_recorded_as_skipped(self):
        result = classify_result(UNIT_A, completed(passed=0, skipped=2), AT)

        assert result.finished.outcome == "skipped"

    def test_a_missing_report_is_recorded_as_error(self):
        result = classify_result(
            UNIT_A,
            {
                "status": "completed",
                "ok": False,
                "job_id": "j",
                "summary": None,
            },
            AT,
        )

        assert result.finished.outcome == "error"
        assert not result.problem

    def test_a_pass_alongside_a_skip_is_still_a_pass(self):
        # Only a status outside passed/failed/skipped makes a run
        # unclassifiable. A skipped Examples row next to a passing one does
        # not: that run passed.
        result = classify_result(UNIT_A, completed(passed=1, skipped=1), AT)

        assert result.finished.outcome == "passed"
        assert not result.problem

    def test_an_unclassifiable_result_becomes_error_with_a_reason(self):
        # An unrecognised scenario status: the CLI raised here. A scheduler
        # has nobody to raise at, so it records the oddity and keeps going.
        result = classify_result(UNIT_A, completed(passed=1, unknown=1), AT)

        assert result.finished.outcome == "error"
        assert result.problem
        assert "classify" in result.problem

    def test_an_unclassifiable_result_keeps_the_job_id(self):
        result = classify_result(
            UNIT_A, completed(passed=1, unknown=1, job_id="job-xyz"), AT
        )

        assert result.finished.job_id == "job-xyz"

    def test_a_pass_contradicted_by_ok_false_is_flagged(self):
        result = classify_result(UNIT_A, completed(passed=1, ok=False), AT)

        assert result.finished.outcome == "error"
        assert result.problem

    def test_junk_is_recorded_rather_than_raised(self):
        result = classify_result(UNIT_A, "not a payload", AT)

        assert result.finished.outcome == "error"
        assert result.problem


class TestPlanTick:
    def test_a_created_campaign_starts_nothing(self):
        tick = plan_tick(records=plan(UNIT_A), lanes=[], max_lanes=4, at=AT)

        assert tick.idle
        assert tick.lifecycle == CREATED

    def test_a_running_campaign_fills_every_free_lane(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            LifecycleRecord(RUNNING_STATE, AT),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=2, at=AT)

        assert len(tick.start) == 2
        assert tick.lifecycle == RUNNING_STATE

    def test_it_never_opens_more_lanes_than_the_limit(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            LifecycleRecord(RUNNING_STATE, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1")]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=2, at=AT)

        assert len(tick.start) == 1

    def test_a_full_window_starts_nothing(self):
        records = [*plan(UNIT_A, UNIT_B), LifecycleRecord(RUNNING_STATE, AT)]
        lanes = [
            Lane(unit=UNIT_A, job_id="job1"),
            Lane(unit=UNIT_B, job_id="job2"),
        ]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=2, at=AT)

        assert tick.start == ()

    def test_a_finished_lane_is_recorded(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=2, at=AT)

        assert [c.finished.outcome for c in tick.record] == ["passed"]

    def test_a_freed_lane_is_refilled_in_the_same_tick(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=1, at=AT)

        assert [c.finished.unit for c in tick.record] == [UNIT_A]
        assert tick.start == (UNIT_B,)

    def test_a_unit_in_flight_is_never_started_twice(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1")]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert UNIT_A not in tick.start

    def test_a_failed_unit_is_not_retried_on_its_own(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "passed"),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=4, at=AT)

        assert tick.start == ()
        assert tick.lifecycle == COMPLETE

    def test_a_paused_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
            LifecycleRecord(PAUSED, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert [c.finished.outcome for c in tick.record] == ["passed"]
        assert tick.start == ()
        assert tick.lifecycle == PAUSED

    def test_a_cancelled_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
            LifecycleRecord(CANCELLED, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert len(tick.record) == 1
        assert tick.start == ()
        assert tick.lifecycle == CANCELLED

    def test_the_last_finished_lane_completes_the_campaign(self):
        records = [
            *plan(UNIT_A),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert tick.lifecycle == COMPLETE
        assert tick.start == ()

    def test_an_unclassifiable_result_does_not_stop_scheduling(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [
            Lane(
                unit=UNIT_A,
                job_id="job1",
                result=completed(passed=1, unknown=1),
            )
        ]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=2, at=AT)

        assert tick.record[0].problem
        # UNIT_A was just recorded as error. It must not be re-queued: only
        # the untouched unit is started.
        assert tick.start == (UNIT_B,)


@pytest.mark.parametrize("state", [PAUSED, CANCELLED])
def test_no_lane_is_ever_opened_outside_running(state):
    records = [
        *plan(UNIT_A, UNIT_B),
        LifecycleRecord(RUNNING_STATE, AT),
        LifecycleRecord(state, AT),
    ]

    assert plan_tick(records=records, lanes=[], max_lanes=8, at=AT).start == ()


class TestSchedulerNeverRetries:
    """A non-passing unit is only ever re-run when someone asks for it."""

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_a_problem_unit_is_never_started_automatically(self, state):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, state),
            attempt(UNIT_B, "passed"),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=8, at=AT)

        assert tick.start == ()

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_problem_units_do_not_crowd_out_untouched_ones(self, state):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, state),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=1, at=AT)

        assert tick.start == (UNIT_B,)

    def test_a_lane_that_errors_frees_capacity_for_untouched_work(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            LifecycleRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed(passed=0))]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=1, at=AT)

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
