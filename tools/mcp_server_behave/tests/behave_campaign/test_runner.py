"""The runner, driven by calling tick directly rather than via the thread.

The thread is tested separately and holds no logic, so everything about
scheduling behaviour is exercised synchronously here.
"""

import itertools
import threading
import time
from typing import NamedTuple

import pytest

from behave_campaign.adapters import JsonlCampaignStore, JsonlEventLog
from behave_campaign.domain.records import (
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    CampaignState,
    Filters,
    Intent,
    IntentRecord,
    PlanRecord,
    RepoState,
    RetryRecord,
    Unit,
    UnitState,
)
from behave_campaign.domain.state import reduce_units, state_of
from behave_campaign.ports import CampaignLockedError
from behave_campaign.runner import KILLED_REASON, CampaignRunner, ThreadTicker
from behave_mcp.domain.pools import Pool
from tests.conftest import FakeLanes, FakeLock, FakeTicker

AT = "2026-09-12T12:00:00Z"


# An SRU bug number, as campaigns are named; any valid id will do.
CAMPAIGN = "1234567"


OTHER_CAMPAIGN = "7654321"


def _wait_until(predicate, timeout=20.0):
    """Block until ``predicate`` holds, so thread tests never race."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), "timed out waiting for the runner"


UNITS = [
    Unit("features/a.feature", "A", "jammy", "lxd-container"),
    Unit("features/b.feature", "B", "noble", "lxd-vm"),
    Unit("features/c.feature", "C", "focal", "lxd-container"),
]
WSL_UNITS = [
    Unit("features/w.feature", "W", "jammy", "wsl"),
    Unit("features/w.feature", "W", "noble", "wsl"),
]


def written_intents(store, campaign_id=CAMPAIGN):
    return [
        record.intent
        for record in store.replay(campaign_id)
        if isinstance(record, IntentRecord)
    ]


@pytest.fixture
def store(tmp_path):
    return JsonlCampaignStore(tmp_path / "campaigns")


@pytest.fixture
def lanes():
    return FakeLanes()


@pytest.fixture
def lock():
    return FakeLock()


@pytest.fixture
def ticker():
    return FakeTicker()


@pytest.fixture
def events(tmp_path):
    return JsonlEventLog(tmp_path / "campaigns")


@pytest.fixture
def runner(store, lanes, lock, ticker, events):
    return CampaignRunner(
        store=store,
        lanes=lanes,
        lock=lock,
        events=events,
        now=lambda: AT,
        ticker=ticker,
    )


def make_runner(store, lanes=None, lock=None, **kwargs):
    return CampaignRunner(
        store=store,
        lanes=lanes if lanes is not None else FakeLanes(),
        lock=lock if lock is not None else FakeLock(),
        events=kwargs.pop("events", None) or JsonlEventLog(store.root),
        now=kwargs.pop("now", lambda: AT),
        ticker=kwargs.pop("ticker", None) or FakeTicker(),
        **kwargs,
    )


def create(store, campaign_id=CAMPAIGN, units=None, lxd_lanes=2, lanes=None):
    units = UNITS if units is None else units
    header = CampaignHeader(
        at=AT,
        campaign_id=campaign_id,
        repo=RepoState(root="/repo"),
        filters=Filters(),
        install_from="proposed",
        lanes=lanes if lanes is not None else {Pool.LXD: lxd_lanes},
    )
    store.create(
        campaign_id, header, [PlanRecord(unit=u, at=AT) for u in units]
    )
    return campaign_id


def state(store, campaign_id=CAMPAIGN):
    return state_of(store.replay(campaign_id))


class TestRun:
    def test_it_takes_the_lock_and_runs_the_campaign(
        self, runner, store, lock
    ):
        create(store)

        response = runner.run(CAMPAIGN)

        assert response.state == CampaignState.RUNNING
        assert response.intent == Intent.RUN
        assert lock.acquired == [CAMPAIGN]

    def test_running_twice_writes_the_intent_once(self, runner, store):
        create(store)
        runner.run(CAMPAIGN)

        runner.run(CAMPAIGN)

        assert written_intents(store) == [Intent.RUN]

    def test_a_second_campaign_cannot_schedule_at_the_same_time(
        self, runner, store
    ):
        create(store, CAMPAIGN)
        create(store, OTHER_CAMPAIGN)
        runner.run(CAMPAIGN)

        with pytest.raises(CampaignError, match="only one campaign"):
            runner.run(OTHER_CAMPAIGN)

    def test_running_a_complete_campaign_schedules_nothing(
        self, runner, store, lanes, lock
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1")
        runner.tick(CAMPAIGN)
        runner.pause(CAMPAIGN)

        response = runner.run(CAMPAIGN)

        assert response.state == CampaignState.COMPLETE
        assert runner.active_campaign() is None
        assert lock.acquired == [CAMPAIGN]

    def test_a_complete_campaign_may_run_while_another_schedules(
        self, runner, store, lanes, ticker
    ):
        create(store, CAMPAIGN, units=[UNITS[0]], lxd_lanes=1)
        create(store, OTHER_CAMPAIGN)
        lanes.auto_finish = {}
        runner.run(CAMPAIGN)
        ticker.drive()
        runner.run(OTHER_CAMPAIGN)

        response = runner.run(CAMPAIGN)

        assert response.state == CampaignState.COMPLETE
        assert runner.active_campaign() == OTHER_CAMPAIGN

    def test_a_lock_held_elsewhere_refuses_and_writes_nothing(self, store):
        create(store)
        runner = make_runner(store, lock=FakeLock(taken_by_other=True))

        with pytest.raises(CampaignLockedError):
            runner.run(CAMPAIGN)

        assert state(store) == CampaignState.CREATED

    def test_a_failed_write_gives_the_lock_back(self, runner, store, lock):
        create(store)
        store.path_for(CAMPAIGN).chmod(0o444)

        with pytest.raises(OSError):
            runner.run(CAMPAIGN)

        assert lock.held == set()
        assert runner.active_campaign() is None

    def test_shutdown_lets_go_without_changing_the_campaign(
        self, runner, store, lock
    ):
        create(store)
        runner.run(CAMPAIGN)

        runner.shutdown()

        assert runner.active_campaign() is None
        assert lock.held == set()
        assert state(store) == CampaignState.RUNNING


class TestTick:
    def test_a_created_campaign_starts_nothing(self, runner, store, lanes):
        create(store)

        report = runner.tick(CAMPAIGN)

        assert lanes.started == []
        assert report.state == CampaignState.CREATED

    def test_a_running_campaign_fills_its_lanes(self, runner, store, lanes):
        create(store, lxd_lanes=2)
        runner.run(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        assert len(lanes.started) == 2
        assert report.started == 2
        assert report.lanes_busy == 2

    def test_it_records_an_attempt_in_flight_for_each_lane(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=2)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        statuses = reduce_units(store.replay(CAMPAIGN))
        busy = [s for s in statuses if s.state == UnitState.IN_FLIGHT]

        assert {s.job_id for s in busy} == {"job1", "job2"}

    def test_lanes_carry_the_campaign_install_source_and_repo(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        _, _, install_from, repo_root = lanes.started[0]

        assert install_from == "proposed"
        assert str(repo_root) == "/repo"

    def test_a_full_window_starts_nothing_more(self, runner, store, lanes):
        create(store, lxd_lanes=2)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        assert len(lanes.started) == 2
        assert report.started == 0

    def test_a_finished_lane_is_recorded_and_refilled(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1")

        report = runner.tick(CAMPAIGN)

        assert report.recorded == 1
        assert report.started == 1
        assert lanes.started_units == [UNITS[0], UNITS[1]]

    def test_a_failure_is_recorded_and_does_not_stop_the_campaign(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1", passed=0, failed=1)

        report = runner.tick(CAMPAIGN)

        statuses = reduce_units(store.replay(CAMPAIGN))
        assert [s.state for s in statuses if s.unit == UNITS[0]] == ["failed"]
        assert report.started == 1

    def test_a_failed_unit_is_never_retried_by_the_runner(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1", passed=0, failed=1)
        runner.tick(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        assert lanes.started_units == [UNITS[0]]
        assert report.started == 0
        assert report.state == CampaignState.COMPLETE

    def test_an_unclassifiable_result_is_reported_as_a_problem(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.results["job1"] = {
            "status": "completed",
            "ok": True,
            "job_id": "job1",
            "summary": {
                "scenarios": {"passed": 1, "unknown": 1},
                "features": {},
            },
        }

        report = runner.tick(CAMPAIGN)

        assert report.problems
        assert report.recorded == 1
        statuses = reduce_units(store.replay(CAMPAIGN))
        assert statuses[0].state == "error"

    def test_a_refused_lane_leaves_the_unit_for_a_later_tick(self, store):
        runner = make_runner(store, lanes=FakeLanes(max_starts=1))
        create(store, lxd_lanes=3)
        runner.run(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        assert report.started == 1
        assert report.problems
        statuses = reduce_units(store.replay(CAMPAIGN))
        assert sum(1 for s in statuses if s.state == "unattempted") == 2

    def test_each_pool_fills_its_own_lanes(self, runner, store, lanes):
        create(
            store,
            units=[*WSL_UNITS, *UNITS],
            lanes={Pool.LXD: 2, Pool.WSL: 1},
        )
        runner.run(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        pools = [unit.pool for unit in lanes.started_units]
        assert report.started == 3
        assert pools.count(Pool.LXD) == 2
        assert pools.count(Pool.WSL) == 1

    def test_a_busy_wsl_lane_is_never_doubled(self, runner, store, lanes):
        create(
            store,
            units=[*WSL_UNITS, *UNITS],
            lanes={Pool.LXD: 3, Pool.WSL: 1},
        )
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job2")

        runner.tick(CAMPAIGN)

        started_wsl = [
            unit for unit in lanes.started_units if unit.pool == Pool.WSL
        ]
        assert started_wsl == [WSL_UNITS[0]]

    def test_a_full_pool_does_not_hold_back_the_others(self, store):
        lanes = FakeLanes(full_pools={Pool.WSL})
        events = JsonlEventLog(store.root)
        runner = make_runner(store, lanes=lanes, events=events)
        create(
            store,
            units=[*WSL_UNITS, *UNITS],
            lanes={Pool.LXD: 3, Pool.WSL: 1},
        )
        runner.run(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        assert report.started == 3
        assert lanes.refusals == 1
        starved = events.read(
            CAMPAIGN,
            since_seq=0,
            kinds=["anomaly.capacity_starved"],
            limit=10,
        )
        assert [event.data["pool"] for event in starved] == ["wsl"]

    def test_the_last_lane_finishing_completes_the_campaign(
        self, runner, store, lanes, lock
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1")

        report = runner.tick(CAMPAIGN)

        assert report.state == CampaignState.COMPLETE
        assert report.finished
        assert report.lanes_busy == 0
        # Complete is derived; nothing but the caller's intent is written.
        assert written_intents(store) == [Intent.RUN]

    def test_a_campaign_with_work_left_is_not_finished(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)

        assert not runner.tick(CAMPAIGN).finished

    def test_a_retried_pass_is_watched_until_its_job_ends(
        self, runner, store, lanes
    ):
        # A pass sticks, so the unit reads passed again afterwards, but its
        # new job is still polled and recorded.
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1")
        runner.tick(CAMPAIGN)
        runner.retry_units(CAMPAIGN, filters=Filters(state=("passed",)))
        runner.tick(CAMPAIGN)

        assert state(store) == CampaignState.FINISHING

        lanes.finish("job2", passed=0, failed=1)
        report = runner.tick(CAMPAIGN)

        assert report.recorded == 1
        assert report.state == CampaignState.COMPLETE


class TestPause:
    def test_pause_stops_new_lanes_but_keeps_the_one_in_flight(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        response = runner.pause(CAMPAIGN)

        assert response.state == CampaignState.PAUSING
        assert response.lanes_busy == 1

    def test_a_pausing_campaign_drains_then_is_paused(
        self, runner, store, lanes, lock
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        runner.pause(CAMPAIGN)
        lanes.finish("job1")

        report = runner.tick(CAMPAIGN)

        assert report.recorded == 1
        assert report.started == 0
        assert report.state == CampaignState.PAUSED
        assert report.finished

    def test_pausing_with_nothing_in_flight_releases_the_scheduler(
        self, runner, store, lock
    ):
        create(store)
        runner.run(CAMPAIGN)

        response = runner.pause(CAMPAIGN)

        assert response.state == CampaignState.PAUSED
        assert runner.active_campaign() is None
        assert lock.released == [CAMPAIGN]

    def test_a_paused_campaign_does_not_block_another(self, runner, store):
        create(store, CAMPAIGN)
        create(store, OTHER_CAMPAIGN)
        runner.run(CAMPAIGN)
        runner.pause(CAMPAIGN)

        response = runner.run(OTHER_CAMPAIGN)

        assert response.state == CampaignState.RUNNING

    def test_a_pausing_campaign_still_holds_the_scheduler(self, runner, store):
        create(store, CAMPAIGN)
        create(store, OTHER_CAMPAIGN)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        runner.pause(CAMPAIGN)

        with pytest.raises(CampaignError, match="only one campaign"):
            runner.run(OTHER_CAMPAIGN)

    def test_run_after_pause_starts_lanes_again(self, runner, store, lanes):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.pause(CAMPAIGN)

        runner.run(CAMPAIGN)
        report = runner.tick(CAMPAIGN)

        assert report.started == 1


class TestPauseWithKill:
    def test_it_kills_every_job_in_flight(self, runner, store, lanes):
        create(store, lxd_lanes=2)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        runner.pause(CAMPAIGN, kill=True)

        assert lanes.killed == ["job1", "job2"]

    def test_the_killed_units_are_interrupted_and_queueable(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=2)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        response = runner.pause(CAMPAIGN, kill=True)

        statuses = reduce_units(store.replay(CAMPAIGN))
        killed = [s for s in statuses if s.attempts]
        assert [u.job_id for u in response.interrupted] == ["job1", "job2"]
        assert all(s.attempts[-1].interrupted for s in killed)
        assert all(s.queueable for s in killed)

    def test_the_campaign_is_paused_at_once(self, runner, store, lanes, lock):
        create(store, lxd_lanes=2)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        response = runner.pause(CAMPAIGN, kill=True)

        assert response.state == CampaignState.PAUSED
        assert runner.active_campaign() is None
        assert lock.released == [CAMPAIGN]

    def test_a_killed_job_is_never_recorded_as_an_outcome(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        runner.pause(CAMPAIGN, kill=True)

        report = runner.tick(CAMPAIGN)

        assert report.recorded == 0
        finished = [
            r for r in store.replay(CAMPAIGN) if isinstance(r, AttemptFinished)
        ]
        assert finished == []

    def test_a_job_it_cannot_signal_is_reported(self, store):
        lanes = FakeLanes(unkillable={"job1"})
        runner = make_runner(store, lanes=lanes)
        create(store, lxd_lanes=2)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        response = runner.pause(CAMPAIGN, kill=True)

        assert response.unsignalled == ["job1"]

    def test_running_again_reruns_the_interrupted_units(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        runner.pause(CAMPAIGN, kill=True)
        runner.run(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        assert report.started == 1
        assert lanes.started_units == [UNITS[0], UNITS[0]]


class TestTheWholeLoop:
    """The real ticker driving a real store, with only the jobs faked.

    This is the one test that exercises the scheduler as it actually runs:
    a thread, a sliding window, and a campaign that finishes on its own.
    """

    def test_a_campaign_runs_itself_to_completion(self, store):
        units = [
            Unit("features/f{}.feature".format(n), "S", "jammy", "lxd-vm")
            for n in range(12)
        ]
        create(store, units=units, lxd_lanes=4)
        lanes = FakeLanes(auto_finish={})
        runner = make_runner(
            store, lanes=lanes, ticker=ThreadTicker(interval=0.01)
        )

        runner.run(CAMPAIGN)
        # Wait for the runner to let go, not just for the store to read
        # complete: the ticker releases the campaign after the tick that
        # finished it, so the two are not simultaneous.
        _wait_until(lambda: runner.active_campaign() is None)

        statuses = reduce_units(store.replay(CAMPAIGN))

        assert state(store) == CampaignState.COMPLETE
        assert len(lanes.started) == 12
        assert all(s.state == "passed" for s in statuses)

    def test_it_never_exceeds_the_lane_limit(self, store):
        """Every unit is started exactly once, never twice."""
        units = [
            Unit("features/f{}.feature".format(n), "S", "jammy", "lxd-vm")
            for n in range(20)
        ]
        create(store, units=units, lxd_lanes=3)
        lanes = FakeLanes(auto_finish={})
        runner = make_runner(
            store, lanes=lanes, ticker=ThreadTicker(interval=0.005)
        )

        runner.run(CAMPAIGN)
        _wait_until(lambda: runner.active_campaign() is None)

        started = lanes.started_units

        assert state(store) == CampaignState.COMPLETE
        assert len(started) == len(set(started)) == 20

    def test_pausing_a_live_campaign_stops_it_opening_lanes(self, store):
        units = [
            Unit("features/f{}.feature".format(n), "S", "jammy", "lxd-vm")
            for n in range(40)
        ]
        create(store, units=units, lxd_lanes=2)
        lanes = FakeLanes(auto_finish={})
        runner = make_runner(
            store, lanes=lanes, ticker=ThreadTicker(interval=0.01)
        )

        runner.run(CAMPAIGN)
        _wait_until(lambda: len(lanes.started) >= 2)
        response = runner.pause(CAMPAIGN)
        _wait_until(lambda: runner.active_campaign() is None)
        settled = len(lanes.started)
        time.sleep(0.1)

        assert response.intent == Intent.HOLD
        assert state(store) == CampaignState.PAUSED
        assert len(lanes.started) == settled
        assert settled < len(units)


class TestEvents:
    """What the runner announces as it goes."""

    @staticmethod
    def kinds(events, family="", campaign_id=CAMPAIGN):
        return [
            e.kind
            for e in events.read(campaign_id, since_seq=0, kinds=[], limit=100)
            if e.kind.startswith(family)
        ]

    def test_run_and_pause_announce_each_state_reached(
        self, runner, store, events
    ):
        create(store)
        runner.run(CAMPAIGN)
        runner.pause(CAMPAIGN)
        runner.run(CAMPAIGN)

        assert self.kinds(events) == [
            "campaign.running",
            "campaign.paused",
            "campaign.running",
        ]

    def test_a_drain_is_announced_as_pausing_then_paused(
        self, runner, store, events, lanes
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        runner.pause(CAMPAIGN)
        lanes.finish("job1")
        runner.tick(CAMPAIGN)

        assert self.kinds(events, "campaign.") == [
            "campaign.running",
            "campaign.pausing",
            "campaign.paused",
        ]

    def test_the_last_jobs_are_announced_as_finishing_then_complete(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1")
        runner.tick(CAMPAIGN)

        assert self.kinds(events, "campaign.") == [
            "campaign.running",
            "campaign.finishing",
            "campaign.complete",
        ]

    def test_a_state_event_says_what_caused_it(self, runner, store, events):
        create(store)
        runner.run(CAMPAIGN)

        (running,) = events.read(
            CAMPAIGN, since_seq=0, kinds=["campaign.running"], limit=10
        )

        assert running.data["cause"] == "run"
        assert running.data["from"] == "created"

    def test_completion_is_announced_with_the_counts(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1")
        runner.tick(CAMPAIGN)

        (done,) = events.read(
            CAMPAIGN, since_seq=0, kinds=["campaign.complete"], limit=10
        )

        assert done.data["counts"]["passed"] == 1
        assert done.data["cause"] == "scheduler"

    def test_a_kill_announces_each_interrupted_unit(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        runner.pause(CAMPAIGN, kill=True)

        (interrupted,) = events.read(
            CAMPAIGN, since_seq=0, kinds=["unit.interrupted"], limit=10
        )
        assert interrupted.data["job_id"] == "job1"
        assert interrupted.data["reason"] == KILLED_REASON

    def test_an_opened_lane_is_announced_with_its_job(
        self, runner, store, events, lanes
    ):
        create(store, lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        opened = events.read(
            CAMPAIGN, since_seq=0, kinds=["lane.started"], limit=10
        )

        assert len(opened) == 1
        assert opened[0].data["job_id"] == "job1"
        assert opened[0].data["release"] == UNITS[0].release
        assert opened[0].data["install_from"] == "proposed"

    def test_a_passing_unit_is_announced(self, runner, store, events, lanes):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1")
        runner.tick(CAMPAIGN)

        assert "unit.passed" in self.kinds(events)

    def test_a_failing_unit_carries_its_failing_steps(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.results["job1"] = {
            "status": "completed",
            "ok": False,
            "job_id": "job1",
            "summary": {
                "scenarios": {"passed": 0, "failed": 1, "skipped": 0},
                "features": {},
            },
            "failures": [
                {
                    "step": "Then it works",
                    "status": "failed",
                    "error_message": "it did not",
                }
            ],
        }
        runner.tick(CAMPAIGN)

        failed = events.read(
            CAMPAIGN, since_seq=0, kinds=["unit.failed"], limit=10
        )

        assert len(failed) == 1
        assert failed[0].data["failures"][0]["step"] == "Then it works"
        assert failed[0].data["failures"][0]["error_message"] == "it did not"

    def test_an_unclassifiable_result_has_its_own_kind_and_reason(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.results["job1"] = {
            "status": "completed",
            "ok": True,
            "job_id": "job1",
            "summary": {
                "scenarios": {"passed": 1, "unknown": 1},
                "features": {},
            },
        }
        runner.tick(CAMPAIGN)

        odd = events.read(
            CAMPAIGN, since_seq=0, kinds=["unit.unclassifiable"], limit=10
        )

        assert len(odd) == 1
        assert odd[0].data["problem"]


class TestRetryUnits:
    """Nothing re-runs a failure on its own; this is the only way."""

    def _finish(self, runner, lanes, job_id, **kwargs):
        runner.tick(CAMPAIGN)
        lanes.finish(job_id, **kwargs)
        runner.tick(CAMPAIGN)

    def test_retrying_a_complete_campaign_runs_it_again(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        assert state(store) == CampaignState.COMPLETE

        response = runner.retry_units(CAMPAIGN)

        assert response.requeued == 1
        assert response.state == CampaignState.RUNNING
        assert runner.active_campaign() == CAMPAIGN

    def test_retrying_a_paused_campaign_sets_intent_to_run(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0], UNITS[1]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        lanes.finish("job2")
        runner.tick(CAMPAIGN)
        runner.pause(CAMPAIGN)

        response = runner.retry_units(CAMPAIGN)

        assert response.intent == Intent.RUN
        assert written_intents(store) == [Intent.RUN, Intent.HOLD, Intent.RUN]
        assert runner.tick(CAMPAIGN).started == 1

    def test_the_retried_unit_actually_runs_again(self, runner, store, lanes):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        runner.retry_units(CAMPAIGN)

        report = runner.tick(CAMPAIGN)

        assert report.started == 1
        assert lanes.started_units == [UNITS[0], UNITS[0]]

    def test_a_retry_can_turn_a_failure_into_a_pass(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        runner.retry_units(CAMPAIGN)
        self._finish(runner, lanes, "job2")

        statuses = reduce_units(store.replay(CAMPAIGN))

        assert statuses[0].state == "passed"
        assert not statuses[0].retry_pending

    def test_the_request_is_recorded_with_its_reason(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        self._finish(runner, lanes, "job1", passed=0, failed=1)

        runner.retry_units(CAMPAIGN, reason="looked flaky")

        retries = [
            r for r in store.replay(CAMPAIGN) if isinstance(r, RetryRecord)
        ]
        assert retries[-1].reason == "looked flaky"

    def test_it_defaults_to_the_problem_states(self, runner, store, lanes):
        create(store, lxd_lanes=3)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        lanes.finish("job1", passed=0, failed=1)
        lanes.finish("job2")
        lanes.finish("job3", passed=0, failed=1)
        runner.tick(CAMPAIGN)

        response = runner.retry_units(CAMPAIGN)

        # The passing unit is left alone.
        assert response.requeued == 2
        assert all(u.state == "failed" for u in response.units)

    def test_a_passing_unit_can_be_retried_when_named(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        self._finish(runner, lanes, "job1")

        response = runner.retry_units(
            CAMPAIGN, filters=Filters(state=("passed",))
        )

        assert response.requeued == 1

    def test_filters_narrow_the_selection(self, runner, store, lanes):
        create(store, lxd_lanes=3)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        for job in ("job1", "job2", "job3"):
            lanes.finish(job, passed=0, failed=1)
        runner.tick(CAMPAIGN)

        response = runner.retry_units(
            CAMPAIGN, filters=Filters(release=("jammy",))
        )

        assert {u.release for u in response.units} == {"jammy"}

    def test_a_unit_in_flight_is_never_selected(self, runner, store, lanes):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        with pytest.raises(CampaignError):
            runner.retry_units(
                CAMPAIGN, filters=Filters(state=(UnitState.IN_FLIGHT,))
            )

    def test_matching_nothing_is_rejected(self, runner, store):
        create(store)
        runner.run(CAMPAIGN)

        with pytest.raises(CampaignError, match="nothing was re-queued"):
            runner.retry_units(CAMPAIGN)

    def test_retrying_is_announced(self, runner, store, lanes, events):
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        self._finish(runner, lanes, "job1", passed=0, failed=1)

        runner.retry_units(CAMPAIGN, reason="flaky")

        retried = events.read(
            CAMPAIGN, since_seq=0, kinds=["unit.retried"], limit=10
        )
        assert len(retried) == 1
        assert retried[0].data["previous_state"] == "failed"
        assert retried[0].data["reason"] == "flaky"


class TestSignals:
    """Derived signals reach the event log, and are said once."""

    def test_a_hung_lane_is_reported_once(self, store, lanes, events):
        runner = make_runner(
            store,
            lanes=lanes,
            events=events,
            now=lambda: "2026-09-12T12:00:00Z",
            overdue_seconds=60,
        )
        create(store, units=[UNITS[0]], lxd_lanes=1)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)

        # Time moves on while the job does not.
        runner._now = lambda: "2026-09-12T13:00:00Z"
        runner.tick(CAMPAIGN)
        runner.tick(CAMPAIGN)

        overdue = events.read(
            CAMPAIGN, since_seq=0, kinds=["lane.overdue"], limit=10
        )

        assert len(overdue) == 1
        assert overdue[0].data["job_id"] == "job1"
        assert overdue[0].data["elapsed_seconds"] == 3600

    def test_a_refused_lane_is_reported_as_starvation(self, store, events):
        runner = make_runner(
            store, lanes=FakeLanes(max_starts=1), events=events
        )
        create(store, lxd_lanes=3)
        runner.run(CAMPAIGN)

        runner.tick(CAMPAIGN)

        starved = events.read(
            CAMPAIGN,
            since_seq=0,
            kinds=["anomaly.capacity_starved"],
            limit=10,
        )

        assert len(starved) == 1
        assert starved[0].data["reason"]

    def test_starvation_is_reported_once_per_episode(self, store, events):
        lanes = FakeLanes(full_pools={Pool.LXD})
        runner = make_runner(store, lanes=lanes, events=events)
        campaign_id = create(store, lxd_lanes=1)
        runner.run(campaign_id)

        def starved():
            return events.read(
                campaign_id,
                since_seq=0,
                kinds=["anomaly.capacity_starved"],
                limit=10,
            )

        runner.tick(campaign_id)
        runner.tick(campaign_id)
        runner.tick(campaign_id)
        assert len(starved()) == 1

        lanes.full_pools.clear()
        runner.tick(campaign_id)
        (_, opened_job, _, _) = lanes.started[-1]
        lanes.finish(opened_job)
        lanes.full_pools.add(Pool.LXD)
        runner.tick(campaign_id)
        runner.tick(campaign_id)

        assert len(starved()) == 2

    def test_a_tick_waits_for_a_control_verb_in_progress(self, runner, store):
        create(store)
        runner.run(CAMPAIGN)
        ticked = threading.Event()

        with runner._guard:
            thread = threading.Thread(
                target=lambda: (runner.tick(CAMPAIGN), ticked.set())
            )
            thread.start()
            assert not ticked.wait(0.2)

        assert ticked.wait(5)
        thread.join(5)

    def test_a_scenario_failing_across_releases_is_reported(
        self, runner, store, lanes
    ):
        units = [
            Unit("features/a.feature", "A", release, "lxd-vm")
            for release in ("focal", "jammy", "noble")
        ]
        create(store, units=units, lxd_lanes=3)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        for job in ("job1", "job2", "job3"):
            lanes.finish(job, passed=0, failed=1)
        runner.tick(CAMPAIGN)

        reported = runner._events.read(
            CAMPAIGN,
            since_seq=0,
            kinds=["anomaly.*"],
            limit=10,
        )

        assert [e.kind for e in reported] == [
            "anomaly.repeated_scenario_failure"
        ]
        assert reported[0].data["releases"] == ["focal", "jammy", "noble"]

    def test_a_healthy_campaign_reports_no_anomalies(
        self, runner, store, lanes
    ):
        create(store, lxd_lanes=3)
        runner.run(CAMPAIGN)
        runner.tick(CAMPAIGN)
        for job in ("job1", "job2", "job3"):
            lanes.finish(job)
        runner.tick(CAMPAIGN)

        reported = runner._events.read(
            CAMPAIGN, since_seq=0, kinds=["anomaly.*"], limit=10
        )

        assert reported == []


class TestThreadTicker:
    """The one place a real thread runs. It decides nothing."""

    @staticmethod
    def _wait_until_done(ticker, timeout=5.0):
        deadline = time.monotonic() + timeout
        while ticker.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        ticker.stop()

    def test_it_ticks_until_the_tick_reports_it_is_finished(self):
        calls = []

        def tick():
            calls.append(1)
            return len(calls) >= 3

        ticker = ThreadTicker(interval=0.01)
        ticker.start(tick)
        self._wait_until_done(ticker)

        assert len(calls) == 3
        assert not ticker.is_running()

    def test_the_first_tick_does_not_wait_for_the_interval(self):
        # A campaign must start filling lanes at once, not one interval late.
        fired = threading.Event()
        ticker = ThreadTicker(interval=30.0)
        ticker.start(lambda: bool(fired.set()) or True)

        assert fired.wait(timeout=5)
        ticker.stop()

    def test_stopping_ends_the_loop(self):
        ticker = ThreadTicker(interval=0.01)
        ticker.start(lambda: False)

        ticker.stop()

        assert not ticker.is_running()

    def test_a_failing_tick_does_not_kill_the_loop(self):
        calls = []

        def tick():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            return True

        ticker = ThreadTicker(interval=0.01)
        ticker.start(tick)
        self._wait_until_done(ticker)

        assert len(calls) == 3

    def test_starting_twice_does_not_run_two_loops(self):
        calls = []
        ticker = ThreadTicker(interval=0.01)
        ticker.start(lambda: calls.append(1) or False)

        ticker.start(lambda: calls.append(1) or False)

        assert ticker.is_running()
        ticker.stop()


# Each named state, arranged on a two-unit campaign of units A and B.
IN_FLIGHT_JOB = "job-in-flight"


DONE_JOB = "job-done"


def _start(unit, job_id):
    return AttemptStarted(
        unit=unit, job_id=job_id, install_from="local", at=AT
    )


def _tried(unit, job_id, outcome):
    return [
        _start(unit, job_id),
        AttemptFinished(unit=unit, job_id=job_id, outcome=outcome, at=AT),
    ]


A, B = UNITS[0], UNITS[1]


ARRANGED = {
    CampaignState.CREATED: [],
    CampaignState.RUNNING: [
        IntentRecord(Intent.RUN, AT),
        _start(A, IN_FLIGHT_JOB),
    ],
    CampaignState.FINISHING: [
        IntentRecord(Intent.RUN, AT),
        *_tried(A, DONE_JOB, "passed"),
        _start(B, IN_FLIGHT_JOB),
    ],
    CampaignState.COMPLETE: [
        IntentRecord(Intent.RUN, AT),
        *_tried(A, DONE_JOB, "passed"),
        *_tried(B, IN_FLIGHT_JOB, "failed"),
    ],
    CampaignState.PAUSING: [
        IntentRecord(Intent.HOLD, AT),
        _start(A, IN_FLIGHT_JOB),
    ],
    CampaignState.PAUSED: [
        IntentRecord(Intent.HOLD, AT),
        *_tried(A, DONE_JOB, "failed"),
    ],
}


def test_every_state_has_an_arrangement():
    assert set(ARRANGED) == set(CampaignState)


@pytest.mark.parametrize("named, records", ARRANGED.items(), ids=str)
def test_each_arrangement_is_the_state_it_names(store, named, records):
    campaign_id = create(store, units=[A, B])

    store.append(campaign_id, records)

    assert state(store, campaign_id) == named


class Refused(NamedTuple):
    reason: str


VERBS = {
    "run": lambda runner, campaign_id: runner.run(campaign_id),
    "pause": lambda runner, campaign_id: runner.pause(campaign_id),
    "pause-kill": lambda runner, campaign_id: runner.pause(
        campaign_id, kill=True
    ),
    "retry": lambda runner, campaign_id: runner.retry_units(campaign_id),
}


# What each verb leaves each named state in. Retry with no filter selects
# the problem units, so it is refused where there are none.
CONTROL = {
    (CampaignState.CREATED, "run"): CampaignState.RUNNING,
    (CampaignState.CREATED, "pause"): CampaignState.PAUSED,
    (CampaignState.CREATED, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.CREATED, "retry"): Refused("no units matched"),
    (CampaignState.RUNNING, "run"): CampaignState.RUNNING,
    (CampaignState.RUNNING, "pause"): CampaignState.PAUSING,
    (CampaignState.RUNNING, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.RUNNING, "retry"): Refused("no units matched"),
    (CampaignState.FINISHING, "run"): CampaignState.FINISHING,
    (CampaignState.FINISHING, "pause"): CampaignState.PAUSING,
    (CampaignState.FINISHING, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.FINISHING, "retry"): Refused("no units matched"),
    (CampaignState.COMPLETE, "run"): CampaignState.COMPLETE,
    (CampaignState.COMPLETE, "pause"): CampaignState.COMPLETE,
    (CampaignState.COMPLETE, "pause-kill"): CampaignState.COMPLETE,
    (CampaignState.COMPLETE, "retry"): CampaignState.RUNNING,
    (CampaignState.PAUSING, "run"): CampaignState.RUNNING,
    (CampaignState.PAUSING, "pause"): CampaignState.PAUSING,
    (CampaignState.PAUSING, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.PAUSING, "retry"): Refused("no units matched"),
    (CampaignState.PAUSED, "run"): CampaignState.RUNNING,
    (CampaignState.PAUSED, "pause"): CampaignState.PAUSED,
    (CampaignState.PAUSED, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.PAUSED, "retry"): CampaignState.RUNNING,
}


def test_every_state_and_verb_has_an_expectation():
    assert set(CONTROL) == set(itertools.product(CampaignState, VERBS))


@pytest.mark.parametrize(
    "named, verb, expected",
    [(named, verb, want) for (named, verb), want in CONTROL.items()],
    ids=str,
)
def test_verbs_by_state(runner, store, named, verb, expected):
    campaign_id = create(store, units=[A, B])
    store.append(campaign_id, ARRANGED[named])

    if isinstance(expected, Refused):
        with pytest.raises(CampaignError, match=expected.reason):
            VERBS[verb](runner, campaign_id)
        assert state(store, campaign_id) == named
    else:
        VERBS[verb](runner, campaign_id)
        assert state(store, campaign_id) == expected
