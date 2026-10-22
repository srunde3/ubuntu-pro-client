"""The events a campaign's records announce."""

import pytest

from pro_behave.campaign.domain.feed import derive_feed
from pro_behave.campaign.domain.records import (
    CampaignRecord,
    Cause,
    Failure,
    FinishedRecord,
    Intent,
    IntentRecord,
    InterruptedRecord,
    Outcome,
    OverdueRecord,
    PlanRecord,
    RecordType,
    RetryRecord,
    Scope,
    StartedRecord,
    StarvedRecord,
    Unit,
)
from pro_behave.jobs.domain.pools import Pool

AT = "2026-09-12T12:00:00Z"


# An SRU bug number, as campaigns are named; any valid id will do.
CAMPAIGN = "1234567"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")


JOB_A = "job-a"


JOB_B = "job-b"


HEADER = CampaignRecord(
    at=AT,
    campaign_id=CAMPAIGN,
    scope=Scope(release=("jammy", "noble")),
    install_from="proposed",
    max_jobs={Pool.LXD: 2},
)


def created(*units):
    return [HEADER, *(PlanRecord(unit=unit, at=AT) for unit in units)]


def run():
    return IntentRecord(Intent.RUN, Cause.RUN, AT)


def hold():
    return IntentRecord(Intent.HOLD, Cause.PAUSE, AT)


def start(unit, job_id):
    return StartedRecord(unit, job_id, install_from="proposed", at=AT)


def finish(unit, job_id, outcome, **extra):
    return FinishedRecord(unit, job_id, outcome, at=AT, **extra)


def kinds(records, family=""):
    return [
        event.kind
        for event in derive_feed(CAMPAIGN, records)
        if event.kind.startswith(family)
    ]


# A running campaign with unit A's job in flight: the context each record
# below is appended to.
RUNNING = [*created(UNIT_A, UNIT_B), run(), start(UNIT_A, JOB_A)]


# The events each kind of record announces of its own, apart from any
# change of the campaign's state it causes. The header and plan are
# announced together as campaign.created.
OWN_EVENTS = {
    RecordType.STARTED: (start(UNIT_B, JOB_B), ["job.started"]),
    RecordType.FINISHED: (
        finish(UNIT_A, JOB_A, Outcome.FAILED),
        ["unit.failed"],
    ),
    RecordType.INTERRUPTED: (
        InterruptedRecord(UNIT_A, JOB_A, at=AT, reason="killed"),
        ["unit.interrupted"],
    ),
    RecordType.INTENT: (hold(), []),
    RecordType.RETRY: (RetryRecord(UNIT_B, at=AT), ["unit.retried"]),
    RecordType.STARVED: (
        StarvedRecord(UNIT_B, Pool.LXD, reason="pool is full", at=AT),
        ["anomaly.starved"],
    ),
    RecordType.OVERDUE: (
        OverdueRecord(UNIT_A, JOB_A, elapsed_seconds=3600, at=AT),
        ["job.overdue"],
    ),
}


def test_every_record_type_has_an_expectation():
    head = {RecordType.CAMPAIGN, RecordType.PLAN}
    assert set(OWN_EVENTS) | head == set(RecordType)


@pytest.mark.parametrize(
    "kind, record, expected",
    [(kind, record, want) for kind, (record, want) in OWN_EVENTS.items()],
    ids=str,
)
def test_each_record_announces_its_own_event(kind, record, expected):
    before = len(derive_feed(CAMPAIGN, RUNNING))

    events = derive_feed(CAMPAIGN, [*RUNNING, record])[before:]

    own = [e.kind for e in events if not e.kind.startswith("campaign.")]
    assert own == expected


def test_creation_is_announced_with_the_scope():
    (event,) = derive_feed(CAMPAIGN, created(UNIT_A, UNIT_B))

    assert event.kind == "campaign.created"
    assert event.data == {
        "total_units": 2,
        "install_from": "proposed",
        "max_jobs": {"lxd": 2},
        "scope": HEADER.scope.as_dict(),
    }


@pytest.mark.parametrize(
    "outcome, kind",
    [
        (Outcome.PASSED, "unit.passed"),
        (Outcome.FAILED, "unit.failed"),
        (Outcome.SKIPPED, "unit.skipped"),
        (Outcome.ERROR, "unit.errored"),
    ],
    ids=str,
)
def test_a_finish_announces_its_outcome(outcome, kind):
    records = [*RUNNING, finish(UNIT_A, JOB_A, outcome)]

    assert kind in kinds(records, "unit.")


def test_a_failure_carries_its_failing_steps():
    failure = Failure("Then it works", "failed", "it did not")
    records = [
        *RUNNING,
        finish(UNIT_A, JOB_A, Outcome.FAILED, failures=(failure,)),
    ]

    (event,) = [
        e for e in derive_feed(CAMPAIGN, records) if e.kind == "unit.failed"
    ]

    assert event.data["failures"] == [failure.as_dict()]
    assert event.data["job_id"] == JOB_A


def test_an_unclassifiable_finish_has_its_own_kind_and_reason():
    records = [
        *RUNNING,
        finish(UNIT_A, JOB_A, Outcome.ERROR, reason="cannot classify"),
    ]

    (event,) = [
        e
        for e in derive_feed(CAMPAIGN, records)
        if e.kind == "unit.unclassifiable"
    ]

    assert event.data["reason"] == "cannot classify"


def test_a_retry_says_what_the_unit_was():
    records = [
        *RUNNING,
        finish(UNIT_A, JOB_A, Outcome.FAILED),
        RetryRecord(UNIT_A, at=AT, reason="flaky"),
    ]

    (event,) = [
        e for e in derive_feed(CAMPAIGN, records) if e.kind == "unit.retried"
    ]

    assert event.data["previous_state"] == "failed"
    assert event.data["reason"] == "flaky"


# Record sequences after creation, and the campaign events they announce.
STATE_CASES = [
    pytest.param([run()], ["campaign.running"], id="run"),
    pytest.param(
        [run(), start(UNIT_A, JOB_A), start(UNIT_B, JOB_B)],
        ["campaign.running", "campaign.finishing"],
        id="last-unit-started",
    ),
    pytest.param(
        [
            run(),
            start(UNIT_A, JOB_A),
            start(UNIT_B, JOB_B),
            finish(UNIT_A, JOB_A, Outcome.PASSED),
            finish(UNIT_B, JOB_B, Outcome.FAILED),
        ],
        ["campaign.running", "campaign.finishing", "campaign.complete"],
        id="last-job-recorded",
    ),
    pytest.param(
        [run(), start(UNIT_A, JOB_A), hold()],
        ["campaign.running", "campaign.pausing"],
        id="pause-with-a-job-in-flight",
    ),
    pytest.param(
        [
            run(),
            start(UNIT_A, JOB_A),
            hold(),
            finish(UNIT_A, JOB_A, Outcome.PASSED),
        ],
        ["campaign.running", "campaign.pausing", "campaign.paused"],
        id="pause-drains",
    ),
    pytest.param([hold()], ["campaign.paused"], id="pause-before-running"),
]


@pytest.mark.parametrize("records, expected", STATE_CASES)
def test_each_change_of_state_is_announced_once(records, expected):
    feed_kinds = kinds([*created(UNIT_A, UNIT_B), *records], "campaign.")

    assert feed_kinds == ["campaign.created", *expected]


def test_a_state_event_says_where_from_why_and_the_counts():
    records = [*created(UNIT_A, UNIT_B), run()]

    (event,) = [
        e
        for e in derive_feed(CAMPAIGN, records)
        if e.kind == "campaign.running"
    ]

    assert event.data == {
        "from": "created",
        "intent": "run",
        "progress": "untouched",
        "cause": "run",
        "counts": {
            "unattempted": 2,
            "in_flight": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "error": 0,
        },
    }


def test_a_retry_that_reopens_a_campaign_is_its_cause():
    complete = [
        *created(UNIT_A),
        run(),
        start(UNIT_A, JOB_A),
        finish(UNIT_A, JOB_A, Outcome.FAILED),
    ]
    records = [
        *complete,
        IntentRecord(Intent.RUN, Cause.RETRY, AT),
        RetryRecord(UNIT_A, at=AT),
    ]

    events = derive_feed(CAMPAIGN, records)[
        len(derive_feed(CAMPAIGN, complete)) :
    ]

    (reopened,) = [e for e in events if e.kind == "campaign.running"]
    assert reopened.data["cause"] == "retry"
    assert reopened.data["from"] == "complete"


def test_one_scenario_failing_on_three_releases_is_announced_once():
    units = [
        Unit("features/a.feature", "A", release, "lxd-vm")
        for release in ("focal", "jammy", "noble", "resolute")
    ]
    records = [*created(*units), run()]
    for number, unit in enumerate(units):
        job_id = "job-{}".format(number)
        records += [start(unit, job_id), finish(unit, job_id, Outcome.FAILED)]

    (event,) = [
        e
        for e in derive_feed(CAMPAIGN, records)
        if e.kind == "anomaly.repeated_scenario_failure"
    ]

    assert event.data["releases"] == ["focal", "jammy", "noble"]


def test_a_run_of_skips_is_announced_once():
    units = [
        Unit("features/s{}.feature".format(n), "S", "jammy", "lxd-vm")
        for n in range(6)
    ]
    records = [*created(*units), run()]
    for number, unit in enumerate(units):
        job_id = "job-{}".format(number)
        records += [
            start(unit, job_id),
            finish(unit, job_id, Outcome.SKIPPED),
        ]

    assert kinds(records).count("anomaly.repeated_skips") == 1


def test_seq_is_dense_from_one():
    records = [
        *RUNNING,
        finish(UNIT_A, JOB_A, Outcome.PASSED),
        start(UNIT_B, JOB_B),
    ]

    feed = derive_feed(CAMPAIGN, records)

    assert [event.seq for event in feed] == list(range(1, len(feed) + 1))


def test_appending_records_never_renumbers_earlier_events():
    records = [
        *RUNNING,
        finish(UNIT_A, JOB_A, Outcome.FAILED),
        RetryRecord(UNIT_A, at=AT),
        start(UNIT_A, JOB_B),
        finish(UNIT_A, JOB_B, Outcome.PASSED),
    ]
    whole = derive_feed(CAMPAIGN, records)

    for end in range(len(created(UNIT_A, UNIT_B)), len(records)):
        prefix = derive_feed(CAMPAIGN, records[:end])

        assert whole[: len(prefix)] == prefix
