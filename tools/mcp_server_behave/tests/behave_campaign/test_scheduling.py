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
    AttemptRecord,
    CampaignHeader,
    Lane,
    PlanRecord,
    StateRecord,
    Unit,
    classify_result,
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


def attempt(unit, state, job_id="job1"):
    return AttemptRecord(
        unit=unit, state=state, job_id=job_id, install_from="proposed", at=AT
    )


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
            StateRecord(state=RUNNING_STATE, at=AT),
            StateRecord(state=PAUSED, at=AT),
        ]

        assert lifecycle(records) == PAUSED

    def test_a_resumed_campaign_is_running_again(self):
        records = [
            *plan(UNIT_A),
            StateRecord(state=RUNNING_STATE, at=AT),
            StateRecord(state=PAUSED, at=AT),
            StateRecord(state=RUNNING_STATE, at=AT),
        ]

        assert lifecycle(records) == RUNNING_STATE

    def test_running_with_work_left_stays_running(self):
        records = [*plan(UNIT_A, UNIT_B), StateRecord(RUNNING_STATE, AT)]

        assert lifecycle(records) == RUNNING_STATE

    def test_running_with_a_job_in_flight_stays_running(self):
        records = [
            *plan(UNIT_A),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]

        assert lifecycle(records) == RUNNING_STATE

    def test_running_with_nothing_left_is_complete(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "passed"),
            attempt(UNIT_B, "passed"),
        ]

        assert lifecycle(records) == COMPLETE

    def test_problem_units_do_not_count_as_remaining_work(self):
        # Nothing retries a failure on its own, so a campaign whose units
        # all finished is complete even when some of them failed.
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "skipped"),
        ]

        assert lifecycle(records) == COMPLETE

    def test_a_paused_campaign_with_nothing_left_stays_paused(self):
        records = [
            *plan(UNIT_A),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "passed"),
            StateRecord(PAUSED, AT),
        ]

        assert lifecycle(records) == PAUSED

    def test_a_cancelled_campaign_stays_cancelled(self):
        records = [
            *plan(UNIT_A),
            StateRecord(RUNNING_STATE, AT),
            StateRecord(CANCELLED, AT),
        ]

        assert lifecycle(records) == CANCELLED

    def test_the_reason_of_the_latest_transition_is_reported(self):
        records = [
            *plan(UNIT_A),
            StateRecord(RUNNING_STATE, AT),
            StateRecord(PAUSED, AT, reason="server_restart"),
        ]

        assert last_state_reason(records) == "server_restart"

    def test_no_transition_has_no_reason(self):
        assert last_state_reason(plan(UNIT_A)) == ""


class TestClassifyResult:
    def test_a_clean_pass_is_recorded_as_passed(self):
        result = classify_result(UNIT_A, completed(passed=2), AT)

        assert result.attempt.state == "passed"
        assert result.attempt.job_id == "job1"
        assert result.attempt.at == AT
        assert not result.problem

    def test_any_failure_is_recorded_as_failed(self):
        result = classify_result(UNIT_A, completed(passed=1, failed=1), AT)

        assert result.attempt.state == "failed"
        assert not result.problem

    def test_a_skip_only_run_is_recorded_as_skipped(self):
        result = classify_result(UNIT_A, completed(passed=0, skipped=2), AT)

        assert result.attempt.state == "skipped"

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

        assert result.attempt.state == "error"
        assert not result.problem

    def test_a_pass_alongside_a_skip_is_still_a_pass(self):
        # Only a status outside passed/failed/skipped makes a run
        # unclassifiable. A skipped Examples row next to a passing one does
        # not: that run passed.
        result = classify_result(UNIT_A, completed(passed=1, skipped=1), AT)

        assert result.attempt.state == "passed"
        assert not result.problem

    def test_an_unclassifiable_result_becomes_error_with_a_reason(self):
        # An unrecognised scenario status: the CLI raised here. A scheduler
        # has nobody to raise at, so it records the oddity and keeps going.
        result = classify_result(UNIT_A, completed(passed=1, unknown=1), AT)

        assert result.attempt.state == "error"
        assert result.problem
        assert "classify" in result.problem

    def test_an_unclassifiable_result_keeps_the_job_id(self):
        result = classify_result(
            UNIT_A, completed(passed=1, unknown=1, job_id="job-xyz"), AT
        )

        assert result.attempt.job_id == "job-xyz"

    def test_a_pass_contradicted_by_ok_false_is_flagged(self):
        result = classify_result(UNIT_A, completed(passed=1, ok=False), AT)

        assert result.attempt.state == "error"
        assert result.problem

    def test_junk_is_recorded_rather_than_raised(self):
        result = classify_result(UNIT_A, "not a payload", AT)

        assert result.attempt.state == "error"
        assert result.problem


class TestPlanTick:
    def test_a_created_campaign_starts_nothing(self):
        tick = plan_tick(records=plan(UNIT_A), lanes=[], max_lanes=4, at=AT)

        assert tick.idle
        assert tick.lifecycle == CREATED

    def test_a_running_campaign_fills_every_free_lane(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            StateRecord(RUNNING_STATE, AT),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=2, at=AT)

        assert len(tick.start) == 2
        assert tick.lifecycle == RUNNING_STATE

    def test_it_never_opens_more_lanes_than_the_limit(self):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            StateRecord(RUNNING_STATE, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1")]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=2, at=AT)

        assert len(tick.start) == 1

    def test_a_full_window_starts_nothing(self):
        records = [*plan(UNIT_A, UNIT_B), StateRecord(RUNNING_STATE, AT)]
        lanes = [
            Lane(unit=UNIT_A, job_id="job1"),
            Lane(unit=UNIT_B, job_id="job2"),
        ]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=2, at=AT)

        assert tick.start == ()

    def test_a_finished_lane_is_recorded(self):
        records = [
            *plan(UNIT_A),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=2, at=AT)

        assert [c.attempt.state for c in tick.record] == ["passed"]

    def test_a_freed_lane_is_refilled_in_the_same_tick(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=1, at=AT)

        assert [c.attempt.unit for c in tick.record] == [UNIT_A]
        assert tick.start == (UNIT_B,)

    def test_a_unit_in_flight_is_never_started_twice(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1")]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert UNIT_A not in tick.start

    def test_a_failed_unit_is_not_retried_on_its_own(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "failed"),
            attempt(UNIT_B, "passed"),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=4, at=AT)

        assert tick.start == ()
        assert tick.lifecycle == COMPLETE

    def test_a_paused_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
            StateRecord(PAUSED, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert [c.attempt.state for c in tick.record] == ["passed"]
        assert tick.start == ()
        assert tick.lifecycle == PAUSED

    def test_a_cancelled_campaign_records_but_starts_nothing(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
            StateRecord(CANCELLED, AT),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert len(tick.record) == 1
        assert tick.start == ()
        assert tick.lifecycle == CANCELLED

    def test_the_last_finished_lane_completes_the_campaign(self):
        records = [
            *plan(UNIT_A),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed())]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=4, at=AT)

        assert tick.lifecycle == COMPLETE
        assert tick.start == ()

    def test_an_unclassifiable_result_does_not_stop_scheduling(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
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
        StateRecord(RUNNING_STATE, AT),
        StateRecord(state, AT),
    ]

    assert plan_tick(records=records, lanes=[], max_lanes=8, at=AT).start == ()


class TestSchedulerNeverRetries:
    """A non-passing unit is only ever re-run when someone asks for it."""

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_a_problem_unit_is_never_started_automatically(self, state):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, state),
            attempt(UNIT_B, "passed"),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=8, at=AT)

        assert tick.start == ()

    @pytest.mark.parametrize("state", ["failed", "skipped", "error"])
    def test_problem_units_do_not_crowd_out_untouched_ones(self, state):
        records = [
            *plan(UNIT_A, UNIT_B, UNIT_C),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, state),
        ]

        tick = plan_tick(records=records, lanes=[], max_lanes=1, at=AT)

        assert tick.start == (UNIT_B,)

    def test_a_lane_that_errors_frees_capacity_for_untouched_work(self):
        records = [
            *plan(UNIT_A, UNIT_B),
            StateRecord(RUNNING_STATE, AT),
            attempt(UNIT_A, "running"),
        ]
        lanes = [Lane(unit=UNIT_A, job_id="job1", result=completed(passed=0))]

        tick = plan_tick(records=records, lanes=lanes, max_lanes=1, at=AT)

        assert tick.record[0].attempt.state == "error"
        assert tick.start == (UNIT_B,)
