"""The runner, driven by calling tick directly rather than via the thread.

The thread is tested separately and holds no logic, so everything about
scheduling behaviour is exercised synchronously here.
"""

import threading
import time

import pytest

from behave_campaign.adapters import JsonlCampaignStore, JsonlEventLog
from behave_campaign.domain import (
    CampaignError,
    CampaignHeader,
    Filters,
    Lifecycle,
    LifecycleRecord,
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

        assert response.lifecycle == Lifecycle.RUNNING
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
        assert lifecycle(store.replay("1234567")) == Lifecycle.COMPLETE

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
        assert lifecycle(store.replay("1234567")) == Lifecycle.CREATED


class TestTick:
    def test_a_created_campaign_starts_nothing(self, runner, store, lanes):
        create(store)

        report = runner.tick("1234567")

        assert lanes.started == []
        assert report.lifecycle == Lifecycle.CREATED

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
        assert report.lifecycle == Lifecycle.COMPLETE

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

        assert report.lifecycle == Lifecycle.COMPLETE
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

        assert response.lifecycle == Lifecycle.PAUSED
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

        assert response.lifecycle == Lifecycle.CANCELLED
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


class TestReopen:
    """Taking a cancellation back, for one made by mistake."""

    @staticmethod
    def _restarted(store, lanes, lock, events):
        """A second runner over the same files: a server that came back."""
        return CampaignRunner(
            store=store,
            lanes=lanes,
            lock=lock,
            events=events,
            now=lambda: AT,
            ticker=FakeTicker(),
        )

    def test_it_puts_a_cancelled_campaign_back_to_work(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.cancel("1234567")

        response = runner.reopen("1234567", reason="cancelled by mistake")
        report = runner.tick("1234567")

        assert response.lifecycle == Lifecycle.RUNNING
        assert response.rescheduling
        assert report.started == 1

    def test_the_cancellation_stays_in_the_record(self, runner, store):
        create(store)
        runner.start("1234567")
        runner.cancel("1234567")

        runner.reopen("1234567", reason="cancelled by mistake")

        assert [
            record.state
            for record in store.replay("1234567")
            if isinstance(record, LifecycleRecord)
        ] == [Lifecycle.RUNNING, Lifecycle.CANCELLED, Lifecycle.RUNNING]

    def test_the_reason_is_what_the_campaign_now_reports(self, runner, store):
        create(store)
        runner.start("1234567")
        runner.cancel("1234567")

        response = runner.reopen("1234567", reason="cancelled by mistake")

        assert response.reason == "cancelled by mistake"

    def test_it_takes_the_lock_again(self, runner, store, lock):
        create(store)
        runner.start("1234567")
        runner.cancel("1234567")
        assert "1234567" not in lock.held

        runner.reopen("1234567")

        assert "1234567" in lock.held

    def test_a_finished_campaign_comes_back_complete_and_retryable(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1", passed=0, failed=1)
        runner.tick("1234567")
        runner.cancel("1234567")

        response = runner.reopen("1234567")

        # Nothing to schedule until someone asks for the failure again,
        # which is exactly what reopening it was for.
        assert response.lifecycle == Lifecycle.COMPLETE
        assert not response.rescheduling
        assert runner.retry_units("1234567").requeued == 1

    def test_reopening_a_campaign_that_was_not_cancelled_is_rejected(
        self, runner, store
    ):
        create(store)
        runner.start("1234567")

        with pytest.raises(CampaignError) as error:
            runner.reopen("1234567")

        assert "not cancelled" in str(error.value)

    def test_lanes_still_draining_are_left_to_report_themselves(
        self, runner, store, lanes
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        runner.cancel("1234567")

        response = runner.reopen("1234567")
        lanes.finish("job1")
        report = runner.tick("1234567")

        assert response.abandoned == []
        assert response.lanes_busy == 1
        assert report.recorded == 1

    def test_jobs_nothing_is_watching_stop_a_reopen(
        self, runner, store, lanes, lock, events
    ):
        create(store, max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        runner.cancel("1234567")
        restarted = self._restarted(store, lanes, lock, events)

        with pytest.raises(CampaignError) as error:
            restarted.reopen("1234567")

        assert "nothing is watching" in str(error.value)
        assert UNITS[0].scenario in str(error.value)

    def test_abandoning_them_records_them_as_errored(
        self, runner, store, lanes, lock, events
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        runner.cancel("1234567")
        restarted = self._restarted(store, lanes, lock, events)

        response = restarted.reopen("1234567", abandon_in_flight=True)

        assert [unit.scenario for unit in response.abandoned] == [
            UNITS[0].scenario
        ]
        assert response.counts.running == 0
        assert response.counts.error == 1
        assert [
            status.state for status in reduce_units(store.replay("1234567"))
        ] == ["error"]


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
        assert lifecycle(records) == Lifecycle.PAUSED

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

        assert report.lifecycle == Lifecycle.RUNNING
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

        assert lifecycle(records) == Lifecycle.COMPLETE
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

        assert lifecycle(store.replay("1234567")) == Lifecycle.COMPLETE
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

        assert response.lifecycle == Lifecycle.PAUSED
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

    def test_reopening_is_announced_with_its_reason(
        self, runner, store, events
    ):
        create(store)
        runner.start("1234567")
        runner.cancel("1234567")
        runner.reopen("1234567", reason="cancelled by mistake")

        reopened = events.read(
            "1234567", since_seq=0, kinds=["campaign.reopened"], limit=10
        )

        assert len(reopened) == 1
        assert reopened[0].data["reason"] == "cancelled by mistake"
        assert reopened[0].data["abandoned"] == 0

    def test_an_abandoned_unit_is_announced_as_errored(
        self, runner, store, lanes, lock, events
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")
        runner.cancel("1234567")
        restarted = TestReopen._restarted(store, lanes, lock, events)

        restarted.reopen("1234567", abandon_in_flight=True)

        errored = events.read(
            "1234567", since_seq=0, kinds=["unit.errored"], limit=10
        )

        assert len(errored) == 1
        assert errored[0].data["job_id"] == "job1"
        assert errored[0].data["problem"] == "abandoned_on_reopen"

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


class TestRetryUnits:
    """Nothing re-runs a failure on its own; this is the only way."""

    def _finish(self, runner, lanes, job_id, **kwargs):
        runner.tick("1234567")
        lanes.finish(job_id, **kwargs)
        runner.tick("1234567")

    def test_a_failed_unit_becomes_schedulable_again(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        assert lifecycle(store.replay("1234567")) == Lifecycle.COMPLETE

        response = runner.retry_units("1234567")

        assert response.requeued == 1
        assert response.lifecycle == Lifecycle.RUNNING
        assert response.rescheduling

    def test_the_retried_unit_actually_runs_again(self, runner, store, lanes):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        runner.retry_units("1234567")

        report = runner.tick("1234567")

        assert report.started == 1
        assert lanes.started_units == [UNITS[0], UNITS[0]]

    def test_a_retry_can_turn_a_failure_into_a_pass(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        runner.retry_units("1234567")
        self._finish(runner, lanes, "job2")

        statuses = reduce_units(store.replay("1234567"))

        assert statuses[0].state == "passed"
        assert not statuses[0].retry_pending

    def test_the_request_is_recorded_with_its_reason(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)

        runner.retry_units("1234567", reason="looked flaky")

        stored = store.replay("1234567")[-1]
        assert stored.reason == "looked flaky"

    def test_it_defaults_to_the_problem_states(self, runner, store, lanes):
        create(store, max_lanes=3)
        runner.start("1234567")
        runner.tick("1234567")
        lanes.finish("job1", passed=0, failed=1)
        lanes.finish("job2")
        lanes.finish("job3", passed=0, failed=1)
        runner.tick("1234567")

        response = runner.retry_units("1234567")

        # The passing unit is left alone.
        assert response.requeued == 2
        assert all(u.state == "failed" for u in response.units)

    def test_a_passing_unit_can_be_retried_when_named(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1")

        response = runner.retry_units(
            "1234567", filters=Filters(state=("passed",))
        )

        assert response.requeued == 1

    def test_filters_narrow_the_selection(self, runner, store, lanes):
        create(store, max_lanes=3)
        runner.start("1234567")
        runner.tick("1234567")
        for job in ("job1", "job2", "job3"):
            lanes.finish(job, passed=0, failed=1)
        runner.tick("1234567")

        response = runner.retry_units(
            "1234567", filters=Filters(release=("jammy",))
        )

        assert {u.release for u in response.units} == {"jammy"}

    def test_a_unit_in_flight_is_never_selected(self, runner, store, lanes):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")

        with pytest.raises(CampaignError):
            runner.retry_units("1234567", filters=Filters(state=("running",)))

    def test_matching_nothing_is_rejected(self, runner, store):
        create(store)
        runner.start("1234567")

        with pytest.raises(CampaignError) as error:
            runner.retry_units("1234567")

        assert "nothing was re-queued" in str(error.value)

    def test_a_cancelled_campaign_refuses_retries(self, runner, store, lanes):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        runner.cancel("1234567")

        with pytest.raises(CampaignError) as error:
            runner.retry_units("1234567")

        assert "cancelled" in str(error.value)

    def test_a_paused_campaign_accepts_retries_but_stays_paused(
        self, runner, store, lanes
    ):
        # Two units, so the campaign is still running -- and so pausable --
        # once the first one has failed.
        create(store, units=[UNITS[0], UNITS[1]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)
        runner.pause("1234567")

        response = runner.retry_units("1234567")

        assert response.requeued == 1
        assert response.lifecycle == Lifecycle.PAUSED
        assert not response.rescheduling
        assert runner.tick("1234567").started == 0

    def test_cancelling_a_finished_campaign_closes_it_to_retries(
        self, runner, store, lanes
    ):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)

        runner.cancel("1234567")

        with pytest.raises(CampaignError):
            runner.retry_units("1234567")

    def test_retrying_is_announced(self, runner, store, lanes, events):
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        self._finish(runner, lanes, "job1", passed=0, failed=1)

        runner.retry_units("1234567", reason="flaky")

        retried = events.read(
            "1234567", since_seq=0, kinds=["unit.retried"], limit=10
        )
        assert len(retried) == 1
        assert retried[0].data["previous_state"] == "failed"
        assert retried[0].data["reason"] == "flaky"


class TestSignals:
    """Derived signals reach the event log, and are said once."""

    def test_a_hung_lane_is_reported_once(self, store, lanes, lock, events):
        runner = CampaignRunner(
            store=store,
            lanes=lanes,
            lock=lock,
            events=events,
            now=lambda: "2026-09-12T12:00:00Z",
            ticker=FakeTicker(),
            overdue_seconds=60,
        )
        create(store, units=[UNITS[0]], max_lanes=1)
        runner.start("1234567")
        runner.tick("1234567")

        # Time moves on while the job does not.
        runner._now = lambda: "2026-09-12T13:00:00Z"
        runner.tick("1234567")
        runner.tick("1234567")

        overdue = events.read(
            "1234567", since_seq=0, kinds=["lane.overdue"], limit=10
        )

        assert len(overdue) == 1
        assert overdue[0].data["job_id"] == "job1"
        assert overdue[0].data["elapsed_seconds"] == 3600

    def test_a_refused_lane_is_reported_as_starvation(
        self, store, lock, events
    ):
        lanes = FakeLanes(max_starts=1)
        runner = CampaignRunner(
            store=store,
            lanes=lanes,
            lock=lock,
            events=events,
            now=lambda: AT,
            ticker=FakeTicker(),
        )
        create(store, max_lanes=3)
        runner.start("1234567")

        runner.tick("1234567")

        starved = events.read(
            "1234567",
            since_seq=0,
            kinds=["anomaly.capacity_starved"],
            limit=10,
        )

        assert len(starved) == 1
        assert starved[0].data["reason"]

    def test_a_scenario_failing_across_releases_is_reported(
        self, runner, store, lanes
    ):
        units = [
            Unit("features/a.feature", "A", release, "lxd-vm")
            for release in ("focal", "jammy", "noble")
        ]
        create(store, units=units, max_lanes=3)
        runner.start("1234567")
        runner.tick("1234567")
        for job in ("job1", "job2", "job3"):
            lanes.finish(job, passed=0, failed=1)
        runner.tick("1234567")

        reported = runner._events.read(
            "1234567",
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
        create(store, max_lanes=3)
        runner.start("1234567")
        runner.tick("1234567")
        for job in ("job1", "job2", "job3"):
            lanes.finish(job)
        runner.tick("1234567")

        reported = runner._events.read(
            "1234567", since_seq=0, kinds=["anomaly.*"], limit=10
        )

        assert reported == []
