"""Unit states, lifecycle, filters and counts derived from records."""

import pytest

from behave_campaign.domain.records import (
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    Filters,
    Lifecycle,
    LifecycleRecord,
    PlanRecord,
    RetryRecord,
    Unit,
)
from behave_campaign.domain.state import (
    count_states,
    last_state_reason,
    lifecycle,
    outstanding,
    problems,
    reduce_units,
    running,
    select_next,
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


class TestReduceUnits:
    def test_planned_unit_without_attempts_is_unattempted(self):
        statuses = reduce_units([PlanRecord(UNIT_A)])

        assert statuses[0].state == "unattempted"
        assert statuses[0].job_id is None

    def test_passing_attempt_wins_over_earlier_and_later_failures(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                attempt(UNIT_A, "failed", "job-1"),
                attempt(UNIT_A, "passed", "job-2"),
                attempt(UNIT_A, "failed", "job-3"),
            ]
        )

        assert statuses[0].state == "passed"
        assert statuses[0].job_id == "job-2"
        assert len(statuses[0].attempts) == 3

    def test_latest_attempt_wins_until_a_unit_passes(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                attempt(UNIT_A, "failed", "job-1"),
                attempt(UNIT_A, "skipped", "job-2"),
            ]
        )

        assert statuses[0].state == "skipped"
        assert statuses[0].job_id == "job-2"

    def test_units_are_ordered_and_deduplicated(self):
        statuses = reduce_units(
            [PlanRecord(UNIT_B), PlanRecord(UNIT_A), PlanRecord(UNIT_A)]
        )

        assert [status.unit for status in statuses] == [UNIT_A, UNIT_B]

    def test_attempts_for_unplanned_units_are_not_reported(self):
        statuses = reduce_units([attempt(UNIT_A, "failed", "job-1")])

        assert statuses == []


class TestSelectNext:
    def test_unattempted_units_come_before_retryable_problems(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "failed", "job-1"),
            ]
        )

        selected = select_next(statuses, limit=5)

        assert [status.unit for status in selected] == [UNIT_B, UNIT_A]

    def test_passed_and_running_units_are_excluded(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "passed", "job-1"),
                attempt(UNIT_B, "running", "job-2"),
            ]
        )

        assert select_next(statuses, limit=5) == []

    def test_limit_must_be_positive(self):
        with pytest.raises(CampaignError):
            select_next([], limit=0)


class TestFilters:
    def test_state_filter_applies_to_current_state(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "failed", "job-1"),
            ]
        )
        filters = Filters(state=("failed",))

        kept = [s.unit for s in statuses if filters.matches(s)]

        assert kept == [UNIT_A]

    def test_counts_cover_every_state(self):
        counts = count_states(reduce_units([PlanRecord(UNIT_A)]))

        assert counts["unattempted"] == 1
        assert counts["passed"] == 0

    def test_running_and_problems_extract_matching_statuses(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "running", "job-1"),
                attempt(UNIT_B, "failed", "job-2"),
            ]
        )

        assert [s.unit for s in running(statuses)] == [UNIT_A]
        assert [s.unit for s in problems(statuses)] == [UNIT_B]
