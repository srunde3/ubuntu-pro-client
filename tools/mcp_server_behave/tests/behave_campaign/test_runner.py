"""The runner, driven by calling tick directly rather than via the thread.

The thread is tested separately and holds no logic, so everything about
scheduling behaviour is exercised synchronously here.
"""

import threading
import time

import pytest

from behave_campaign.adapters import JsonlCampaignStore, JsonlEventLog
from behave_campaign.domain import (
    CANCELLED,
    COMPLETE,
    CREATED,
    PAUSED,
    RUNNING_STATE,
    CampaignError,
    CampaignHeader,
    Filters,
    PlanRecord,
    RepoState,
    Unit,
    lifecycle,
    reduce_units,
)
from behave_campaign.ports import CampaignLockedError, LaneStartError
from behave_campaign.runner import RESTART_REASON, CampaignRunner, ThreadTicker

AT = "2026-09-12T12:00:00Z"


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


def completed(passed=1, failed=0, job_id="job"):
    return {
        "status": "completed",
        "ok": failed == 0,
        "job_id": job_id,
        "summary": {
            "scenarios": {"passed": passed, "failed": failed, "skipped": 0},
            "features": {},
        },
    }


class FakeLanes:
    """Records what was started, and finishes jobs only when told to."""

    def __init__(self, max_starts=None):
        self.started = []
        self.results = {}
        self.max_starts = max_starts
        self.refusals = 0

    def start(self, unit, *, repo_root, install_from):
        if (
            self.max_starts is not None
            and len(self.started) >= self.max_starts
        ):
            self.refusals += 1
            raise LaneStartError("at capacity")
        job_id = "job{}".format(len(self.started) + 1)
        self.started.append((unit, job_id, install_from, repo_root))
        return job_id

    def poll(self, job_id):
        return self.results.get(job_id)

    def finish(self, job_id, **kwargs):
        self.results[job_id] = completed(job_id=job_id, **kwargs)

    @property
    def started_units(self):
        return [unit for unit, _, _, _ in self.started]


class FakeTicker:
    """Captures the tick callable without ever calling it.

    Tests drive ``runner.tick`` themselves, so nothing races them.
    """

    def __init__(self):
        self.tick = None
        self.stopped = 0

    def start(self, tick):
        self.tick = tick

    def stop(self):
        self.stopped += 1
        self.tick = None

    def is_running(self):
        return self.tick is not None


class FakeLock:
    def __init__(self, taken_by_other=False):
        self.held = set()
        self.acquired = []
        self.released = []
        self._taken_by_other = taken_by_other

    def acquire(self, campaign_id):
        if self._taken_by_other:
            raise CampaignLockedError("held elsewhere")
        self.held.add(campaign_id)
        self.acquired.append(campaign_id)

    def release(self, campaign_id):
        self.held.discard(campaign_id)
        self.released.append(campaign_id)

    def held_by_other(self, campaign_id):
        return self._taken_by_other


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


def create(store, campaign_id="1234567", units=None, max_lanes=2):
    units = UNITS if units is None else units
    header = CampaignHeader(
        at=AT,
        campaign_id=campaign_id,
        repo=RepoState(root="/repo"),
        filters=Filters(),
        install_from="proposed",
        max_lanes=max_lanes,
    )
    store.create(
        campaign_id, header, [PlanRecord(unit=u, at=AT) for u in units]
    )
    return campaign_id


class TestStart:
    def test_it_takes_the_lock_and_marks_the_campaign_running(
        self, runner, store, lock
    ):
        create(store)

        response = runner.start("1234567")

        assert response.lifecycle == RUNNING_STATE
        assert lock.acquired == ["1234567"]

    def test_a_second_campaign_cannot_run_at_the_same_time(
        self, runner, store
    ):
        create(store, "111")
        create(store, "222")
        runner.start("111")

        with pytest.raises(CampaignError) as error:
            runner.start("222")

        assert "only one campaign" in str(error.value)

    def test_a_cancelled_campaign_cannot_be_restarted(self, runner, store):
        create(store)
        runner.cancel("1234567")

        with pytest.raises(CampaignError) as error:
            runner.start("1234567")

        assert "cancelled" in str(error.value)

    def test_a_finished_campaign_cannot_be_started(self, runner, store, lanes):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1")
        runner.tick("1234567")
        assert lifecycle(store.replay("1234567")) == COMPLETE

        with pytest.raises(CampaignError) as error:
            runner.start("1234567")

        assert "no work left" in str(error.value)

    def test_a_lock_held_elsewhere_refuses_the_start(self, store, lanes):
        create(store)
        runner = CampaignRunner(
            store=store,
            lanes=lanes,
            lock=FakeLock(taken_by_other=True),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=FakeTicker(),
        )

        with pytest.raises(CampaignLockedError):
            runner.start("1234567")

        # Nothing was recorded, so the campaign is untouched.
        assert lifecycle(store.replay("1234567")) == CREATED


class TestTick:
    def test_a_created_campaign_starts_nothing(self, runner, store, lanes):
        create(store)

        report = runner.tick("1234567")

        assert lanes.started == []
        assert report.lifecycle == CREATED

    def test_a_running_campaign_fills_its_lanes(self, runner, store, lanes):
        create(store, max_lanes=2)
        runner.start("1234567")

        report = runner.tick("1234567")

        assert len(lanes.started) == 2
        assert report.started == 2
        assert report.lanes_busy == 2

    def test_it_records_a_running_attempt_for_each_lane(
        self, runner, store, lanes
    ):
        create(store, max_lanes=2)
        runner.start("1234567")
        runner.tick("1234567")

        statuses = reduce_units(store.replay("1234567"))
        running = [s for s in statuses if s.state == "running"]

        assert len(running) == 2
        assert {s.job_id for s in running} == {"job1", "job2"}

    def test_lanes_carry_the_campaign_install_source_and_repo(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")

        _, _, install_from, repo_root = lanes.started[0]

        assert install_from == "proposed"
        assert str(repo_root) == "/repo"

    def test_a_full_window_starts_nothing_more(self, runner, store, lanes):
        create(store, max_lanes=2)
        runner.start("1234567")
        runner.tick("1234567")

        report = runner.tick("1234567")

        assert len(lanes.started) == 2
        assert report.started == 0

    def test_a_finished_lane_is_recorded_and_refilled(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1")

        report = runner.tick("1234567")

        assert report.recorded == 1
        assert report.started == 1
        assert lanes.started_units == [UNITS[0], UNITS[1]]

    def test_a_failure_is_recorded_and_does_not_stop_the_campaign(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1", passed=0, failed=1)

        report = runner.tick("1234567")

        statuses = reduce_units(store.replay("1234567"))
        assert [s.state for s in statuses if s.unit == UNITS[0]] == ["failed"]
        assert report.started == 1

    def test_a_failed_unit_is_never_retried_by_the_runner(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1", passed=0, failed=1)
        runner.tick("1234567")

        report = runner.tick("1234567")

        assert lanes.started_units == [UNITS[0]]
        assert report.started == 0
        assert report.lifecycle == COMPLETE

    def test_an_unclassifiable_result_is_reported_as_a_problem(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.results["job1"] = {
            "status": "completed",
            "ok": True,
            "job_id": "job1",
            "summary": {
                "scenarios": {"passed": 1, "unknown": 1},
                "features": {},
            },
        }

        report = runner.tick("1234567")

        assert report.problems
        assert report.recorded == 1
        statuses = reduce_units(store.replay("1234567"))
        assert statuses[0].state == "error"

    def test_a_refused_lane_leaves_the_unit_for_a_later_tick(
        self, store, lock
    ):
        lanes = FakeLanes(max_starts=1)
        runner = CampaignRunner(
            store=store,
            lanes=lanes,
            lock=lock,
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=FakeTicker(),
        )
        create(store, max_lanes=3)
        runner.start("1234567")

        report = runner.tick("1234567")

        assert report.started == 1
        assert report.problems
        statuses = reduce_units(store.replay("1234567"))
        assert sum(1 for s in statuses if s.state == "unattempted") == 2

    def test_the_last_lane_finishing_completes_the_campaign(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1")

        report = runner.tick("1234567")

        assert report.lifecycle == COMPLETE
        assert report.finished
        assert report.lanes_busy == 0

    def test_a_campaign_with_work_left_is_not_finished(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")

        assert not runner.tick("1234567").finished


class TestPauseAndResume:
    def test_pause_stops_new_lanes_but_keeps_the_running_one(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")

        response = runner.pause("1234567")

        assert response.lifecycle == PAUSED
        assert response.lanes_busy == 1

    def test_a_paused_campaign_drains_rather_than_abandoning_work(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        runner.pause("1234567")
        lanes.finish("job1")

        report = runner.tick("1234567")

        assert report.recorded == 1
        assert report.started == 0
        statuses = reduce_units(store.replay("1234567"))
        assert statuses[0].state == "passed"

    def test_resume_starts_lanes_again(self, runner, store, lanes):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.pause("1234567")

        runner.resume("1234567")
        report = runner.tick("1234567")

        assert report.started == 1

    def test_pausing_a_created_campaign_is_rejected(self, runner, store):
        create(store)

        with pytest.raises(CampaignError) as error:
            runner.pause("1234567")

        assert "created" in str(error.value)

    def test_resuming_a_running_campaign_is_rejected(self, runner, store):
        create(store)
        runner.start("1234567")

        with pytest.raises(CampaignError) as error:
            runner.resume("1234567")

        assert "not paused" in str(error.value)


class TestCancel:
    def test_cancel_closes_the_campaign_to_scheduling(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")

        response = runner.cancel("1234567")
        report = runner.tick("1234567")

        assert response.lifecycle == CANCELLED
        assert report.started == 0
        assert lanes.started == []

    def test_cancel_drains_a_lane_already_in_flight(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        response = runner.cancel("1234567")
        assert response.lanes_busy == 1

        lanes.finish("job1")
        report = runner.tick("1234567")

        assert report.recorded == 1
        assert report.finished

    def test_cancelling_with_nothing_in_flight_releases_the_lock(
        self, runner, store, lock
    ):
        create(store)
        runner.start("1234567")

        runner.cancel("1234567")

        assert "1234567" not in lock.held

    def test_a_cancelled_campaign_cannot_be_cancelled_again(
        self, runner, store
    ):
        create(store)
        runner.cancel("1234567")

        with pytest.raises(CampaignError):
            runner.cancel("1234567")


class TestRecover:
    def test_a_campaign_left_running_comes_back_paused(self, runner, store):
        create(store)
        runner.start("1234567")

        # A fresh process: nothing in memory, only what is on disk.
        fresh = CampaignRunner(
            store=store,
            lanes=FakeLanes(),
            lock=FakeLock(),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=FakeTicker(),
        )
        paused = fresh.recover()

        assert paused == ["1234567"]
        records = store.replay("1234567")
        assert lifecycle(records) == PAUSED

    def test_the_pause_says_why(self, runner, store):
        from behave_campaign.domain import last_state_reason

        create(store)
        runner.start("1234567")
        CampaignRunner(
            store=store,
            lanes=FakeLanes(),
            lock=FakeLock(),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=FakeTicker(),
        ).recover()

        assert last_state_reason(store.replay("1234567")) == RESTART_REASON

    def test_campaigns_that_were_not_running_are_left_alone(
        self, runner, store
    ):
        create(store, "111")
        create(store, "222")
        runner.start("111")
        runner.pause("111")

        paused = CampaignRunner(
            store=store,
            lanes=FakeLanes(),
            lock=FakeLock(),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=FakeTicker(),
        ).recover()

        assert paused == []

    def test_a_recovered_campaign_can_be_resumed(self, runner, store, lanes):
        create(store, max_lanes=1)
        runner.start("1234567")
        fresh_lanes = FakeLanes()
        fresh = CampaignRunner(
            store=store,
            lanes=fresh_lanes,
            lock=FakeLock(),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=FakeTicker(),
        )
        fresh.recover()

        fresh.resume("1234567")
        report = fresh.tick("1234567")

        assert report.lifecycle == RUNNING_STATE
        assert report.started == 1


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


class TestTheWholeLoop:
    """The real ticker driving a real store, with only the jobs faked.

    This is the one test that exercises the scheduler as it actually runs:
    a thread, a sliding window, and a campaign that finishes on its own.
    """

    class AutoLanes(FakeLanes):
        """Jobs that complete the moment they are first polled."""

        def poll(self, job_id):
            if job_id not in self.results:
                self.finish(job_id)
                return None
            return self.results[job_id]

    def test_a_campaign_runs_itself_to_completion(self, store, tmp_path):
        units = [
            Unit("features/f{}.feature".format(n), "S", "jammy", "lxd-vm")
            for n in range(12)
        ]
        create(store, units=units, max_lanes=4)
        lanes = self.AutoLanes()
        runner = CampaignRunner(
            store=store,
            lanes=lanes,
            lock=FakeLock(),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=ThreadTicker(interval=0.01),
        )

        runner.start("1234567")
        # Wait for the runner to let go, not just for the store to read
        # complete: the ticker releases the campaign after the tick that
        # finished it, so the two are not simultaneous.
        _wait_until(lambda: runner.active_campaign() is None)

        records = store.replay("1234567")
        statuses = reduce_units(records)

        assert lifecycle(records) == COMPLETE
        assert len(lanes.started) == 12
        assert all(s.state == "passed" for s in statuses)
        assert runner.active_campaign() is None

    def test_it_never_exceeds_the_lane_limit(self, store):
        """Every unit is started exactly once, never twice."""
        units = [
            Unit("features/f{}.feature".format(n), "S", "jammy", "lxd-vm")
            for n in range(20)
        ]
        create(store, units=units, max_lanes=3)
        lanes = self.AutoLanes()
        runner = CampaignRunner(
            store=store,
            lanes=lanes,
            lock=FakeLock(),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=ThreadTicker(interval=0.005),
        )

        runner.start("1234567")
        _wait_until(lambda: runner.active_campaign() is None)

        started = lanes.started_units

        assert lifecycle(store.replay("1234567")) == COMPLETE
        assert len(started) == len(set(started)) == 20

    def test_pausing_a_live_campaign_stops_it_opening_lanes(self, store):
        units = [
            Unit("features/f{}.feature".format(n), "S", "jammy", "lxd-vm")
            for n in range(40)
        ]
        create(store, units=units, max_lanes=2)
        lanes = self.AutoLanes()
        runner = CampaignRunner(
            store=store,
            lanes=lanes,
            lock=FakeLock(),
            events=JsonlEventLog(store.root),
            now=lambda: AT,
            ticker=ThreadTicker(interval=0.01),
        )

        runner.start("1234567")
        _wait_until(lambda: len(lanes.started) >= 2)
        response = runner.pause("1234567")
        runner.shutdown()
        settled = len(lanes.started)
        time.sleep(0.1)

        assert response.lifecycle == PAUSED
        assert len(lanes.started) == settled
        assert settled < 40


class TestEvents:
    """What the runner announces as it goes."""

    @staticmethod
    def kinds(events, campaign_id="1234567"):
        return [
            e.kind
            for e in events.read(campaign_id, since_seq=0, kinds=[], limit=100)
        ]

    def test_starting_and_pausing_are_announced(self, runner, store, events):
        create(store)
        runner.start("1234567")
        runner.pause("1234567")
        runner.resume("1234567")
        runner.cancel("1234567")

        assert self.kinds(events) == [
            "campaign.started",
            "campaign.paused",
            "campaign.resumed",
            "campaign.cancelled",
        ]

    def test_an_opened_lane_is_announced_with_its_job(
        self, runner, store, events, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")

        opened = events.read(
            "1234567", since_seq=0, kinds=["lane.started"], limit=10
        )

        assert len(opened) == 1
        assert opened[0].data["job_id"] == "job1"
        assert opened[0].data["release"] == UNITS[0].release
        assert opened[0].data["install_from"] == "proposed"

    def test_a_passing_unit_is_announced(self, runner, store, events, lanes):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1")
        runner.tick("1234567")

        assert "unit.passed" in self.kinds(events)
        assert "lane.released" in self.kinds(events)

    def test_a_failing_unit_carries_its_failing_steps(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
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
        runner.tick("1234567")

        failed = events.read(
            "1234567", since_seq=0, kinds=["unit.failed"], limit=10
        )

        assert len(failed) == 1
        assert failed[0].data["failures"][0]["step"] == "Then it works"
        assert failed[0].data["failures"][0]["error_message"] == "it did not"

    def test_an_unclassifiable_result_has_its_own_kind_and_reason(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.results["job1"] = {
            "status": "completed",
            "ok": True,
            "job_id": "job1",
            "summary": {
                "scenarios": {"passed": 1, "unknown": 1},
                "features": {},
            },
        }
        runner.tick("1234567")

        odd = events.read(
            "1234567", since_seq=0, kinds=["unit.unclassifiable"], limit=10
        )

        assert len(odd) == 1
        assert odd[0].data["problem"]

    def test_completion_is_announced_with_the_counts(
        self, runner, store, events, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1")
        runner.tick("1234567")

        done = events.read(
            "1234567", since_seq=0, kinds=["campaign.complete"], limit=10
        )

        assert len(done) == 1
        assert done[0].data["counts"]["passed"] == 1

    def test_a_restart_pause_says_why(self, runner, store, events):
        create(store)
        runner.start("1234567")
        CampaignRunner(
            store=store,
            lanes=FakeLanes(),
            lock=FakeLock(),
            events=events,
            now=lambda: AT,
            ticker=FakeTicker(),
        ).recover()

        paused = events.read(
            "1234567", since_seq=0, kinds=["campaign.paused"], limit=10
        )

        assert paused[0].data["reason"] == RESTART_REASON
