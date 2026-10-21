"""Overdue lanes, and the clock arithmetic behind them."""

import pytest

from behave_campaign.domain.records import Overdue, Unit
from behave_campaign.domain.schedule import Lane
from behave_campaign.domain.signals import elapsed_seconds, overdue_lanes

OPENED_AT = "2026-09-12T12:00:00Z"


# An hour after the lane opened.
HOUR_LATER = "2026-09-12T13:00:00Z"


ONE_HOUR = 3600


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


JOB_ID = "job-a"


def lane(**overrides):
    fields = dict(unit=UNIT_A, job_id=JOB_ID, opened_at=OPENED_AT)
    fields.update(overrides)
    return Lane(**fields)


class TestOverdueLanes:
    def test_a_lane_open_too_long_is_recorded_once(self):
        found = overdue_lanes(
            [lane()], at=HOUR_LATER, overdue_seconds=ONE_HOUR, reported=()
        )

        assert found == [
            Overdue(
                unit=UNIT_A,
                job_id=JOB_ID,
                elapsed_seconds=ONE_HOUR,
                at=HOUR_LATER,
            )
        ]

    def test_a_lane_already_reported_is_not_recorded_again(self):
        found = overdue_lanes(
            [lane()],
            at=HOUR_LATER,
            overdue_seconds=ONE_HOUR,
            reported={JOB_ID},
        )

        assert found == []

    def test_a_young_lane_is_not_recorded(self):
        found = overdue_lanes(
            [lane()],
            at=HOUR_LATER,
            overdue_seconds=ONE_HOUR + 1,
            reported=(),
        )

        assert found == []

    def test_a_finished_lane_is_never_overdue(self):
        finished = lane(result={"status": "completed"})

        found = overdue_lanes(
            [finished], at=HOUR_LATER, overdue_seconds=ONE_HOUR, reported=()
        )

        assert found == []

    def test_an_unreadable_timestamp_is_ignored_rather_than_raised(self):
        found = overdue_lanes(
            [lane(opened_at="whenever")],
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

    @pytest.mark.parametrize(
        "since,now",
        [("nonsense", OPENED_AT), (OPENED_AT, "nonsense"), (None, OPENED_AT)],
    )
    def test_anything_unreadable_is_none(self, since, now):
        assert elapsed_seconds(since, now) is None
