"""Unit states, intent, progress and named campaign states from records."""

import itertools

import pytest

from behave_campaign.domain.records import (
    CampaignError,
    CampaignRecord,
    CampaignState,
    Cause,
    Filters,
    FinishedRecord,
    Intent,
    IntentRecord,
    InterruptedRecord,
    PlanRecord,
    Progress,
    RetryRecord,
    StartedRecord,
    Unit,
    UnitState,
)
from behave_campaign.domain.state import (
    campaign_state,
    count_states,
    in_flight,
    intent,
    problems,
    progress,
    reduce_units,
    state_of,
)

AT = "2026-09-12T12:00:00Z"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")


FIRST_JOB = "job-1"


SECOND_JOB = "job-2"


THIRD_JOB = "job-3"


def plan(*units):
    return [CampaignRecord(at=AT), *(PlanRecord(unit=u, at=AT) for u in units)]


def start(unit, job_id):
    return StartedRecord(
        unit=unit, job_id=job_id, install_from="proposed", at=AT
    )


def finish(unit, job_id, outcome):
    return FinishedRecord(unit=unit, job_id=job_id, outcome=outcome, at=AT)


def tried(unit, job_id, outcome):
    """A whole attempt: its start and its finish."""
    return [start(unit, job_id), finish(unit, job_id, outcome)]


def interrupt(unit, job_id):
    return InterruptedRecord(unit=unit, job_id=job_id, at=AT, reason="kill")


def retry(unit):
    return RetryRecord(unit=unit, at=AT)


# One unit's history, and what it adds up to: (state, queueable).
UNIT_CASES = [
    pytest.param([], (UnitState.UNATTEMPTED, True), id="never-attempted"),
    pytest.param(
        [start(UNIT_A, FIRST_JOB)],
        (UnitState.IN_FLIGHT, False),
        id="in-flight",
    ),
    pytest.param(
        tried(UNIT_A, FIRST_JOB, "passed"),
        (UnitState.PASSED, False),
        id="passed",
    ),
    pytest.param(
        tried(UNIT_A, FIRST_JOB, "failed"),
        (UnitState.FAILED, False),
        id="failed-waits-for-a-retry",
    ),
    pytest.param(
        tried(UNIT_A, FIRST_JOB, "skipped"),
        (UnitState.SKIPPED, False),
        id="skipped",
    ),
    pytest.param(
        tried(UNIT_A, FIRST_JOB, "error"),
        (UnitState.ERROR, False),
        id="error",
    ),
    pytest.param(
        [*tried(UNIT_A, FIRST_JOB, "failed"), retry(UNIT_A)],
        (UnitState.FAILED, True),
        id="failed-and-retried",
    ),
    pytest.param(
        [
            *tried(UNIT_A, FIRST_JOB, "failed"),
            retry(UNIT_A),
            start(UNIT_A, SECOND_JOB),
        ],
        (UnitState.IN_FLIGHT, False),
        id="a-start-answers-the-retry",
    ),
    pytest.param(
        [start(UNIT_A, FIRST_JOB), interrupt(UNIT_A, FIRST_JOB)],
        (UnitState.UNATTEMPTED, True),
        id="interrupted-first-try",
    ),
    pytest.param(
        [
            *tried(UNIT_A, FIRST_JOB, "failed"),
            retry(UNIT_A),
            start(UNIT_A, SECOND_JOB),
            interrupt(UNIT_A, SECOND_JOB),
        ],
        (UnitState.FAILED, True),
        id="interrupted-retry-keeps-the-outcome",
    ),
    pytest.param(
        [
            *tried(UNIT_A, FIRST_JOB, "passed"),
            retry(UNIT_A),
            start(UNIT_A, SECOND_JOB),
        ],
        (UnitState.IN_FLIGHT, False),
        id="retried-pass-is-in-flight",
    ),
    pytest.param(
        [
            *tried(UNIT_A, FIRST_JOB, "passed"),
            retry(UNIT_A),
            *tried(UNIT_A, SECOND_JOB, "failed"),
        ],
        (UnitState.PASSED, False),
        id="a-pass-sticks",
    ),
]


def test_every_unit_state_is_reached_by_a_case():
    assert {case.values[1][0] for case in UNIT_CASES} == set(UnitState)


@pytest.mark.parametrize("history, expected", UNIT_CASES)
def test_unit_state_and_queueable(history, expected):
    (status,) = reduce_units([*plan(UNIT_A), *history])

    assert (status.state, status.queueable) == expected


class TestReduceUnits:
    def test_a_pass_decides_the_job_id(self):
        records = [
            *plan(UNIT_A),
            *tried(UNIT_A, FIRST_JOB, "failed"),
            *tried(UNIT_A, SECOND_JOB, "passed"),
            *tried(UNIT_A, THIRD_JOB, "failed"),
        ]

        (status,) = reduce_units(records)

        assert status.job_id == SECOND_JOB
        assert len(status.attempts) == 3

    def test_a_unit_in_flight_names_the_job_in_flight(self):
        records = [
            *plan(UNIT_A),
            *tried(UNIT_A, FIRST_JOB, "passed"),
            retry(UNIT_A),
            start(UNIT_A, SECOND_JOB),
        ]

        (status,) = reduce_units(records)

        assert status.job_id == SECOND_JOB

    def test_an_interrupted_attempt_is_kept_and_marked(self):
        records = [
            *plan(UNIT_A),
            start(UNIT_A, FIRST_JOB),
            interrupt(UNIT_A, FIRST_JOB),
        ]

        (status,) = reduce_units(records)

        assert [a.interrupted for a in status.attempts] == [True]
        assert status.attempts[0].outcome is None

    def test_an_interruption_with_no_start_is_ignored(self):
        records = [*plan(UNIT_A), interrupt(UNIT_A, FIRST_JOB)]

        (status,) = reduce_units(records)

        assert status.attempts == ()
        assert status.state == UnitState.UNATTEMPTED

    def test_a_finish_with_no_start_is_a_whole_attempt(self):
        records = [*plan(UNIT_A), finish(UNIT_A, FIRST_JOB, "failed")]

        (status,) = reduce_units(records)

        assert status.state == UnitState.FAILED
        assert len(status.attempts) == 1

    def test_units_are_ordered_and_deduplicated(self):
        records = [PlanRecord(UNIT_B), PlanRecord(UNIT_A), PlanRecord(UNIT_A)]

        statuses = reduce_units(records)

        assert [status.unit for status in statuses] == [UNIT_A, UNIT_B]

    def test_attempts_for_unplanned_units_are_not_reported(self):
        statuses = reduce_units(tried(UNIT_A, FIRST_JOB, "failed"))

        assert statuses == []


class TestIntent:
    def test_a_campaign_with_no_intent_record_never_ran(self):
        assert intent(plan(UNIT_A)) == Intent.NEVER_RUN

    def test_the_latest_intent_wins(self):
        records = [
            *plan(UNIT_A),
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            IntentRecord(Intent.HOLD, Cause.PAUSE, AT),
        ]

        assert intent(records) == Intent.HOLD


# A campaign of units A and B: what B's history adds to A's, as progress.
PROGRESS_CASES = [
    pytest.param([], Progress.UNTOUCHED, id="nothing-attempted"),
    pytest.param(
        tried(UNIT_A, FIRST_JOB, "passed"),
        Progress.OPEN,
        id="an-unattempted-unit-is-left",
    ),
    pytest.param(
        [*tried(UNIT_A, FIRST_JOB, "passed"), start(UNIT_B, SECOND_JOB)],
        Progress.FINISHING,
        id="only-a-job-in-flight-is-left",
    ),
    pytest.param(
        [*tried(UNIT_A, FIRST_JOB, "failed"), start(UNIT_B, SECOND_JOB)],
        Progress.FINISHING,
        id="a-problem-unit-is-not-work-left",
    ),
    pytest.param(
        [
            *tried(UNIT_A, FIRST_JOB, "failed"),
            *tried(UNIT_B, SECOND_JOB, "passed"),
        ],
        Progress.SETTLED,
        id="every-unit-has-an-outcome",
    ),
    pytest.param(
        [
            *tried(UNIT_A, FIRST_JOB, "failed"),
            *tried(UNIT_B, SECOND_JOB, "passed"),
            retry(UNIT_A),
        ],
        Progress.OPEN,
        id="a-retry-reopens-it",
    ),
]


def test_every_progress_is_reached_by_a_case():
    assert {case.values[1] for case in PROGRESS_CASES} == set(Progress)


@pytest.mark.parametrize("history, expected", PROGRESS_CASES)
def test_progress(history, expected):
    statuses = reduce_units([*plan(UNIT_A, UNIT_B), *history])

    assert progress(statuses) == expected


# Every combination of intent, progress and "a job is in flight". None
# marks a combination no record this package writes can produce.
NAMED_STATE = {
    (Intent.NEVER_RUN, Progress.UNTOUCHED, False): CampaignState.CREATED,
    (Intent.NEVER_RUN, Progress.UNTOUCHED, True): None,
    (Intent.NEVER_RUN, Progress.OPEN, False): None,
    (Intent.NEVER_RUN, Progress.OPEN, True): None,
    (Intent.NEVER_RUN, Progress.FINISHING, False): None,
    (Intent.NEVER_RUN, Progress.FINISHING, True): None,
    (Intent.NEVER_RUN, Progress.SETTLED, False): None,
    (Intent.NEVER_RUN, Progress.SETTLED, True): None,
    (Intent.RUN, Progress.UNTOUCHED, False): CampaignState.RUNNING,
    (Intent.RUN, Progress.UNTOUCHED, True): None,
    (Intent.RUN, Progress.OPEN, False): CampaignState.RUNNING,
    (Intent.RUN, Progress.OPEN, True): CampaignState.RUNNING,
    (Intent.RUN, Progress.FINISHING, False): None,
    (Intent.RUN, Progress.FINISHING, True): CampaignState.FINISHING,
    (Intent.RUN, Progress.SETTLED, False): CampaignState.COMPLETE,
    (Intent.RUN, Progress.SETTLED, True): None,
    (Intent.HOLD, Progress.UNTOUCHED, False): CampaignState.PAUSED,
    (Intent.HOLD, Progress.UNTOUCHED, True): None,
    (Intent.HOLD, Progress.OPEN, False): CampaignState.PAUSED,
    # Units are left to queue, but jobs are still draining.
    (Intent.HOLD, Progress.OPEN, True): CampaignState.PAUSING,
    (Intent.HOLD, Progress.FINISHING, False): None,
    (Intent.HOLD, Progress.FINISHING, True): CampaignState.PAUSING,
    (Intent.HOLD, Progress.SETTLED, False): CampaignState.COMPLETE,
    (Intent.HOLD, Progress.SETTLED, True): None,
}


def test_every_combination_has_an_expectation():
    assert set(NAMED_STATE) == set(
        itertools.product(Intent, Progress, (False, True))
    )


def test_every_campaign_state_is_reachable():
    assert set(NAMED_STATE.values()) - {None} == set(CampaignState)


@pytest.mark.parametrize(
    "intent_, progress_, busy",
    [key for key, state in NAMED_STATE.items() if state is not None],
    ids=str,
)
def test_campaign_state_names_a_reachable_combination(
    intent_, progress_, busy
):
    expected = NAMED_STATE[(intent_, progress_, busy)]

    assert campaign_state(intent_, progress_, busy) == expected


@pytest.mark.parametrize(
    "intent_, progress_, busy",
    [key for key, state in NAMED_STATE.items() if state is None],
    ids=str,
)
def test_campaign_state_refuses_an_unreachable_combination(
    intent_, progress_, busy
):
    with pytest.raises(CampaignError, match="inconsistent"):
        campaign_state(intent_, progress_, busy)


def test_state_of_reads_the_records():
    records = [
        *plan(UNIT_A, UNIT_B),
        IntentRecord(Intent.HOLD, Cause.PAUSE, AT),
        start(UNIT_A, FIRST_JOB),
    ]

    assert state_of(records) == CampaignState.PAUSING


class TestFilters:
    def test_state_filter_applies_to_current_state(self):
        statuses = reduce_units(
            [*plan(UNIT_A, UNIT_B), *tried(UNIT_A, FIRST_JOB, "failed")]
        )
        filters = Filters(state=("failed",))

        kept = [s.unit for s in statuses if filters.matches(s)]

        assert kept == [UNIT_A]

    def test_counts_cover_every_state(self):
        counts = count_states(reduce_units(plan(UNIT_A)))

        assert set(counts) == {state.value for state in UnitState}
        assert counts["unattempted"] == 1

    def test_in_flight_and_problems_extract_matching_statuses(self):
        statuses = reduce_units(
            [
                *plan(UNIT_A, UNIT_B),
                start(UNIT_A, FIRST_JOB),
                *tried(UNIT_B, SECOND_JOB, "failed"),
            ]
        )

        assert [s.unit for s in in_flight(statuses)] == [UNIT_A]
        assert [s.unit for s in problems(statuses)] == [UNIT_B]
