"""Derived signals: repeated failures, skips and overdue lanes."""

import pytest

from behave_campaign.domain.records import (
    AttemptFinished,
    CampaignHeader,
    PlanRecord,
    Unit,
)
from behave_campaign.domain.schedule import Lane
from behave_campaign.domain.signals import detect_signals, elapsed_seconds

AT = "2026-09-12T12:00:00Z"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


def plan(*units):
    return [CampaignHeader(at=AT), *(PlanRecord(unit=u, at=AT) for u in units)]


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


class TestDetectSignals:
    """Derived observations for a watcher to judge. They act on nothing."""

    def status(self, unit, state, at=AT):
        from behave_campaign.domain.state import reduce_units

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
