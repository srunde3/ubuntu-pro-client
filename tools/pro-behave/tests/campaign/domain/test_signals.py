"""Overdue jobs, and the clock arithmetic behind them."""

import pytest

from pro_behave.campaign.domain.records import OverdueRecord, Unit
from pro_behave.campaign.domain.schedule import InFlightJob
from pro_behave.campaign.domain.signals import elapsed_seconds, overdue_jobs

STARTED_AT = "2026-09-12T12:00:00Z"


# An hour after the job started.
HOUR_LATER = "2026-09-12T13:00:00Z"


ONE_HOUR = 3600


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


JOB_ID = "job-a"


def job(started_at=STARTED_AT, state="running"):
    """A job in flight, as its status reports it."""
    return InFlightJob(
        unit=UNIT_A,
        job_id=JOB_ID,
        result={"state": state, "started_at": started_at},
    )


class TestOverdueJobs:
    def test_a_job_running_too_long_is_recorded_once(self):
        found = overdue_jobs(
            [job()], at=HOUR_LATER, overdue_seconds=ONE_HOUR, reported=()
        )

        assert found == [
            OverdueRecord(
                unit=UNIT_A,
                job_id=JOB_ID,
                elapsed_seconds=ONE_HOUR,
                at=HOUR_LATER,
            )
        ]

    def test_a_job_already_reported_is_not_recorded_again(self):
        found = overdue_jobs(
            [job()],
            at=HOUR_LATER,
            overdue_seconds=ONE_HOUR,
            reported={JOB_ID},
        )

        assert found == []

    def test_a_young_job_is_not_recorded(self):
        found = overdue_jobs(
            [job()],
            at=HOUR_LATER,
            overdue_seconds=ONE_HOUR + 1,
            reported=(),
        )

        assert found == []

    def test_a_queued_job_is_never_overdue(self):
        found = overdue_jobs(
            [job(started_at="", state="queued")],
            at=HOUR_LATER,
            overdue_seconds=ONE_HOUR,
            reported=(),
        )

        assert found == []

    def test_a_finished_job_is_never_overdue(self):
        finished = job(state="finished")

        found = overdue_jobs(
            [finished], at=HOUR_LATER, overdue_seconds=ONE_HOUR, reported=()
        )

        assert found == []

    def test_an_unreadable_timestamp_is_ignored_rather_than_raised(self):
        found = overdue_jobs(
            [job(started_at="whenever")],
            at=HOUR_LATER,
            overdue_seconds=ONE_HOUR,
            reported=(),
        )

        assert found == []


class TestElapsedSeconds:
    def test_it_measures_between_campaign_timestamps(self):
        assert (
            elapsed_seconds("2026-09-12T12:00:00Z", "2026-09-12T12:01:30Z")
            == 90
        )

    def test_it_reads_the_microseconds_every_writer_records(self):
        assert (
            elapsed_seconds(
                "2026-09-12T12:00:00.250000Z", "2026-09-12T12:00:01.000000Z"
            )
            == 0.75
        )

    @pytest.mark.parametrize(
        "since,now",
        [
            ("nonsense", STARTED_AT),
            (STARTED_AT, "nonsense"),
            (None, STARTED_AT),
        ],
    )
    def test_anything_unreadable_is_none(self, since, now):
        assert elapsed_seconds(since, now) is None
