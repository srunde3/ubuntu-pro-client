"""The campaign service's commands and its reconcile, run step by step.

Nothing runs on its own: a test calls ``reconcile`` where a tool call, a CLI
command or a finished job would, and finishes fake jobs when it chooses.
"""

import itertools
from typing import NamedTuple

import pytest

from pro_behave.campaign.adapters import JsonlCampaignStore
from pro_behave.campaign.domain.classify import KILLED, JobStatus
from pro_behave.campaign.domain.events import event_matches
from pro_behave.campaign.domain.feed import derive_feed
from pro_behave.campaign.domain.records import (
    CampaignError,
    CampaignRecord,
    CampaignState,
    Cause,
    Filters,
    FinishedRecord,
    Intent,
    IntentRecord,
    PlanRecord,
    RepoState,
    RetryRecord,
    Scope,
    StartedRecord,
    Unit,
    UnitState,
)
from pro_behave.campaign.domain.state import reduce_units, state_of
from pro_behave.campaign.service import CampaignService
from pro_behave.jobs.domain.pools import Pool
from tests.conftest import FakeFeatureReader, FakeJobRunner

AT = "2026-09-12T12:00:00Z"
AN_HOUR_LATER = "2026-09-12T13:00:00Z"
TWO_HOURS_LATER = "2026-09-12T14:00:00Z"


# SRU bug numbers, as campaigns are named; any valid ids will do.
CAMPAIGN = "1234567"


OTHER_CAMPAIGN = "7654321"


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
        for record in store.read_records(campaign_id)
        if isinstance(record, IntentRecord)
    ]


def records_of(store, kind, campaign_id=CAMPAIGN):
    return [r for r in store.read_records(campaign_id) if isinstance(r, kind)]


class Feed:
    """The events a campaign's records announce, read as a watcher would."""

    def __init__(self, store):
        self._store = store

    def read(self, campaign_id, since_seq=0, kinds=(), limit=100):
        feed = derive_feed(campaign_id, self._store.read_records(campaign_id))
        return [
            event
            for event in feed
            if event.seq > since_seq and event_matches(event.kind, kinds)
        ][:limit]


@pytest.fixture
def store(tmp_path):
    return JsonlCampaignStore(tmp_path / "campaigns")


@pytest.fixture
def jobs():
    return FakeJobRunner()


@pytest.fixture
def events(store):
    return Feed(store)


def make_campaigns(store, jobs=None, **kwargs):
    return CampaignService(
        store=store,
        features=FakeFeatureReader([]),
        now=kwargs.pop("now", lambda: AT),
        repo_state=lambda root: RepoState(root=str(root)),
        jobs=jobs if jobs is not None else FakeJobRunner(),
        **kwargs,
    )


@pytest.fixture
def campaigns(store, jobs):
    return make_campaigns(store, jobs)


def create(
    store, campaign_id=CAMPAIGN, units=None, lxd_max_jobs=2, max_jobs=None
):
    units = UNITS if units is None else units
    header = CampaignRecord(
        at=AT,
        campaign_id=campaign_id,
        repo=RepoState(root="/repo"),
        scope=Scope(),
        install_from="proposed",
        max_jobs=(
            max_jobs if max_jobs is not None else {Pool.LXD: lxd_max_jobs}
        ),
    )
    store.create(
        campaign_id, header, [PlanRecord(unit=u, at=AT) for u in units]
    )
    return campaign_id


def state(store, campaign_id=CAMPAIGN):
    return state_of(store.read_records(campaign_id))


def in_flight(store, campaign_id=CAMPAIGN):
    return [
        status
        for status in reduce_units(store.read_records(campaign_id))
        if status.in_flight
    ]


class TestRun:
    def test_it_queues_the_campaign_s_units_at_once(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=2)

        response = campaigns.run(campaign_id=CAMPAIGN)

        assert response.state == CampaignState.RUNNING
        assert len(jobs.queued) == 2

    def test_it_holds_the_lock_and_starts_queued_jobs(
        self, campaigns, store, jobs
    ):
        create(store)

        campaigns.run(campaign_id=CAMPAIGN)

        assert jobs.locks >= 1
        assert jobs.job_reconciles >= 1

    def test_running_twice_writes_the_intent_once(self, campaigns, store):
        create(store)

        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.run(campaign_id=CAMPAIGN)

        assert written_intents(store) == [Intent.RUN]

    def test_running_a_complete_campaign_queues_nothing(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()

        response = campaigns.run(campaign_id=CAMPAIGN)

        assert response.state == CampaignState.COMPLETE
        assert len(jobs.queued) == 1

    def test_two_campaigns_run_at_once(self, campaigns, store, jobs):
        create(store, CAMPAIGN, lxd_max_jobs=1)
        create(store, OTHER_CAMPAIGN, lxd_max_jobs=1)

        campaigns.run(campaign_id=CAMPAIGN)
        response = campaigns.run(campaign_id=OTHER_CAMPAIGN)

        assert response.state == CampaignState.RUNNING
        assert state(store, CAMPAIGN) == CampaignState.RUNNING
        assert len(jobs.queued) == 2

    def test_without_the_job_service_it_is_refused(self, store):
        create(store)
        campaigns = CampaignService(
            store=store,
            features=FakeFeatureReader([]),
            now=lambda: AT,
            repo_state=lambda root: RepoState(root=str(root)),
        )

        with pytest.raises(CampaignError, match="job service"):
            campaigns.run(campaign_id=CAMPAIGN)


class TestReconcile:
    def test_a_created_campaign_queues_nothing(self, campaigns, store, jobs):
        create(store)

        campaigns.reconcile()

        assert jobs.queued == []
        assert state(store) == CampaignState.CREATED

    def test_it_records_an_attempt_in_flight_for_each_queued_job(
        self, campaigns, store
    ):
        create(store, lxd_max_jobs=2)
        campaigns.run(campaign_id=CAMPAIGN)

        assert {s.job_id for s in in_flight(store)} == {"job1", "job2"}

    def test_the_attempt_is_recorded_before_the_job_is_queued(self, store):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        order = []

        class Watching(FakeJobRunner):
            def queue(self, job_id, unit, **kwargs):
                record = kwargs["record"]

                def recorded():
                    record()
                    order.append(bool(in_flight(store)))

                super().queue(job_id, unit, **{**kwargs, "record": recorded})
                order.append("queued")

        make_campaigns(store, Watching()).run(campaign_id=CAMPAIGN)

        assert order == [True, "queued"]

    def test_jobs_carry_the_campaign_s_install_source_and_repo(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)

        _, _, install_from, repo_root = jobs.queued[0]

        assert install_from == "proposed"
        assert str(repo_root) == "/repo"

    def test_a_campaign_at_its_max_jobs_queues_nothing_more(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=2)
        campaigns.run(campaign_id=CAMPAIGN)

        campaigns.reconcile()

        assert len(jobs.queued) == 2

    def test_an_ended_job_is_recorded_and_its_unit_replaced(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")

        campaigns.reconcile()

        assert len(records_of(store, FinishedRecord)) == 1
        assert jobs.queued_units == [UNITS[0], UNITS[1]]

    def test_a_failure_is_recorded_and_does_not_stop_the_campaign(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1", outcome="failed")

        campaigns.reconcile()

        statuses = reduce_units(store.read_records(CAMPAIGN))
        assert [s.state for s in statuses if s.unit == UNITS[0]] == ["failed"]
        assert len(jobs.queued) == 2

    def test_a_failed_unit_is_never_run_again_on_its_own(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1", outcome="failed")
        campaigns.reconcile()

        campaigns.reconcile()

        assert jobs.queued_units == [UNITS[0]]
        assert state(store) == CampaignState.COMPLETE

    def test_a_job_s_error_is_recorded_with_its_reason(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1", outcome="error", reason="the command exited 2")

        campaigns.reconcile()

        (finished,) = records_of(store, FinishedRecord)
        assert (finished.outcome, finished.reason) == (
            "error",
            "the command exited 2",
        )

    def test_a_refused_unit_stays_unattempted(self, store):
        jobs = FakeJobRunner(full_pools={Pool.LXD})
        create(store, lxd_max_jobs=3)

        make_campaigns(store, jobs).run(campaign_id=CAMPAIGN)

        statuses = reduce_units(store.read_records(CAMPAIGN))
        assert all(s.state == "unattempted" for s in statuses)
        assert not records_of(store, StartedRecord)

    def test_each_pool_fills_its_own_max_jobs(self, campaigns, store, jobs):
        create(
            store,
            units=[*WSL_UNITS, *UNITS],
            max_jobs={Pool.LXD: 2, Pool.WSL: 1},
        )

        campaigns.run(campaign_id=CAMPAIGN)

        pools = [unit.pool for unit in jobs.queued_units]
        assert pools.count(Pool.LXD) == 2
        assert pools.count(Pool.WSL) == 1

    def test_a_refused_pool_does_not_hold_back_the_others(self, store, events):
        jobs = FakeJobRunner(full_pools={Pool.WSL})
        create(
            store,
            units=[*WSL_UNITS, *UNITS],
            max_jobs={Pool.LXD: 3, Pool.WSL: 1},
        )

        make_campaigns(store, jobs).run(campaign_id=CAMPAIGN)

        assert len(jobs.queued) == 3
        assert jobs.refusals == 1
        starved = events.read(CAMPAIGN, kinds=["anomaly.starved"])
        assert [event.data["pool"] for event in starved] == ["wsl"]

    def test_the_last_job_ending_completes_the_campaign(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")

        campaigns.reconcile()

        assert state(store) == CampaignState.COMPLETE
        assert in_flight(store) == []
        # Complete is derived; nothing but the caller's intent is written.
        assert written_intents(store) == [Intent.RUN]

    @pytest.mark.parametrize("reason", ["died", "killed"])
    def test_an_interrupted_job_is_queued_again(
        self, campaigns, store, jobs, reason
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.results["job1"] = JobStatus(
            state="interrupted", job_id="job1", interruption=reason
        )

        campaigns.reconcile()

        (status,) = reduce_units(store.read_records(CAMPAIGN))
        assert status.attempts[0].interrupted
        assert jobs.queued_units == [UNITS[0], UNITS[0]]

    def test_an_attempt_whose_job_was_never_queued_is_queued_again(
        self, campaigns, store, jobs
    ):
        # A crash between recording the attempt and queuing its job.
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        store.append(
            CAMPAIGN,
            [
                IntentRecord(Intent.RUN, Cause.RUN, AT),
                StartedRecord(UNITS[0], "lost", "proposed", AT),
            ],
        )

        campaigns.reconcile()

        (status,) = reduce_units(store.read_records(CAMPAIGN))
        assert status.attempts[0].interrupted
        assert jobs.queued_units == [UNITS[0]]

    def test_a_retried_pass_is_watched_until_its_job_ends(
        self, campaigns, store, jobs
    ):
        # A pass sticks, so the unit reads passed again afterwards, but its
        # new job is still read and recorded.
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()
        campaigns.retry_units(
            campaign_id=CAMPAIGN, filters=Filters(state=("passed",))
        )
        assert state(store) == CampaignState.FINISHING

        jobs.finish("job2", outcome="failed")
        campaigns.reconcile()

        assert len(records_of(store, FinishedRecord)) == 2
        assert state(store) == CampaignState.COMPLETE

    def test_every_campaign_is_reconciled(self, campaigns, store, jobs):
        create(store, CAMPAIGN, units=[UNITS[0]], lxd_max_jobs=1)
        create(store, OTHER_CAMPAIGN, units=[UNITS[1]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.run(campaign_id=OTHER_CAMPAIGN)
        jobs.finish("job1")
        jobs.finish("job2")

        campaigns.reconcile()

        assert state(store, CAMPAIGN) == CampaignState.COMPLETE
        assert state(store, OTHER_CAMPAIGN) == CampaignState.COMPLETE

    def test_it_ends_by_starting_queued_jobs(self, campaigns, store, jobs):
        campaigns.reconcile()

        assert (jobs.locks, jobs.job_reconciles) == (1, 1)

    def test_a_restart_changes_nothing(self, store, jobs):
        # A new service over the same files is all a restart is.
        create(store, lxd_max_jobs=2)
        make_campaigns(store, jobs).run(campaign_id=CAMPAIGN)

        make_campaigns(store, jobs).reconcile()

        assert len(jobs.queued) == 2
        assert len(records_of(store, StartedRecord)) == 2


class TestPause:
    def test_pause_queues_nothing_new_but_keeps_the_job_in_flight(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)

        response = campaigns.pause(campaign_id=CAMPAIGN)
        campaigns.reconcile()

        assert response.state == CampaignState.PAUSING
        assert response.in_flight == 1
        assert len(jobs.queued) == 1

    def test_a_pausing_campaign_drains_then_is_paused(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.pause(campaign_id=CAMPAIGN)
        jobs.finish("job1")

        campaigns.reconcile()

        assert len(records_of(store, FinishedRecord)) == 1
        assert len(jobs.queued) == 1
        assert state(store) == CampaignState.PAUSED

    def test_an_interrupted_unit_is_not_queued_again_while_held(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.pause(campaign_id=CAMPAIGN)
        jobs.results["job1"] = JobStatus(
            state="interrupted", job_id="job1", interruption="died"
        )

        campaigns.reconcile()

        assert len(jobs.queued) == 1
        assert state(store) == CampaignState.PAUSED

    def test_run_after_pause_queues_again(self, campaigns, store, jobs):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.pause(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()

        campaigns.run(campaign_id=CAMPAIGN)

        assert len(jobs.queued) == 2


class TestPauseWithKill:
    def test_it_kills_every_job_in_flight(self, campaigns, store, jobs):
        create(store, lxd_max_jobs=2)
        campaigns.run(campaign_id=CAMPAIGN)

        campaigns.pause(campaign_id=CAMPAIGN, kill=True)

        assert jobs.killed == ["job1", "job2"]

    def test_the_killed_units_are_interrupted_and_queueable(
        self, campaigns, store, jobs
    ):
        create(store, lxd_max_jobs=2)
        campaigns.run(campaign_id=CAMPAIGN)

        response = campaigns.pause(campaign_id=CAMPAIGN, kill=True)

        statuses = reduce_units(store.read_records(CAMPAIGN))
        killed = [s for s in statuses if s.attempts]
        assert [u.job_id for u in response.killed] == ["job1", "job2"]
        assert all(s.attempts[-1].interrupted for s in killed)
        assert all(s.queueable for s in killed)

    def test_the_campaign_is_paused_at_once(self, campaigns, store, jobs):
        create(store, lxd_max_jobs=2)
        campaigns.run(campaign_id=CAMPAIGN)

        response = campaigns.pause(campaign_id=CAMPAIGN, kill=True)

        assert response.state == CampaignState.PAUSED

    def test_a_killed_job_is_never_recorded_as_an_outcome(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.pause(campaign_id=CAMPAIGN, kill=True)

        campaigns.reconcile()

        assert records_of(store, FinishedRecord) == []

    def test_a_job_it_cannot_kill_is_reported(self, store):
        jobs = FakeJobRunner(unkillable={"job1"})
        campaigns = make_campaigns(store, jobs)
        create(store, lxd_max_jobs=2)
        campaigns.run(campaign_id=CAMPAIGN)

        response = campaigns.pause(campaign_id=CAMPAIGN, kill=True)

        assert response.not_killed == ["job1"]

    def test_running_again_reruns_the_interrupted_units(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.pause(campaign_id=CAMPAIGN, kill=True)

        campaigns.run(campaign_id=CAMPAIGN)

        assert jobs.queued_units == [UNITS[0], UNITS[0]]


class TestTheWholeLoop:
    """Reconciles alone carry a campaign to its end, as job processes and
    tool calls do, with only the jobs faked."""

    # A campaign of a few units finishes in a handful of reconciles; this
    # only stops a bug from looping forever.
    MAX_RECONCILES = 200

    def drive(self, campaigns, store, campaign_id=CAMPAIGN):
        for _ in range(self.MAX_RECONCILES):
            if state(store, campaign_id) in (
                CampaignState.COMPLETE,
                CampaignState.PAUSED,
            ):
                return
            campaigns.reconcile()
        raise AssertionError("campaign unfinished after reconciles")

    def units(self, count):
        return [
            Unit("features/f{}.feature".format(n), "S", "jammy", "lxd-vm")
            for n in range(count)
        ]

    def test_a_campaign_runs_to_completion(self, store):
        create(store, units=self.units(12), lxd_max_jobs=4)
        jobs = FakeJobRunner(auto_finish={})
        campaigns = make_campaigns(store, jobs)

        campaigns.run(campaign_id=CAMPAIGN)
        self.drive(campaigns, store)

        statuses = reduce_units(store.read_records(CAMPAIGN))
        assert len(jobs.queued) == 12
        assert all(s.state == "passed" for s in statuses)

    def test_every_unit_is_queued_exactly_once(self, store):
        create(store, units=self.units(20), lxd_max_jobs=3)
        jobs = FakeJobRunner(auto_finish={})
        campaigns = make_campaigns(store, jobs)

        campaigns.run(campaign_id=CAMPAIGN)
        self.drive(campaigns, store)

        queued = jobs.queued_units
        assert len(queued) == len(set(queued)) == 20

    def test_two_campaigns_finish_side_by_side(self, store):
        create(store, CAMPAIGN, units=self.units(4), lxd_max_jobs=2)
        create(store, OTHER_CAMPAIGN, units=self.units(4), lxd_max_jobs=2)
        campaigns = make_campaigns(store, FakeJobRunner(auto_finish={}))

        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.run(campaign_id=OTHER_CAMPAIGN)
        self.drive(campaigns, store, CAMPAIGN)
        self.drive(campaigns, store, OTHER_CAMPAIGN)

        assert state(store, CAMPAIGN) == CampaignState.COMPLETE
        assert state(store, OTHER_CAMPAIGN) == CampaignState.COMPLETE

    def test_pausing_mid_way_stops_queuing(self, store):
        create(store, units=self.units(40), lxd_max_jobs=2)
        jobs = FakeJobRunner(auto_finish={})
        campaigns = make_campaigns(store, jobs)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.reconcile()

        response = campaigns.pause(campaign_id=CAMPAIGN)
        self.drive(campaigns, store)
        settled = len(jobs.queued)
        campaigns.reconcile()

        assert response.intent == Intent.HOLD
        assert state(store) == CampaignState.PAUSED
        assert len(jobs.queued) == settled < 40


class TestEvents:
    """What a campaign announces as it goes."""

    @staticmethod
    def kinds(events, family="", campaign_id=CAMPAIGN):
        return [
            e.kind
            for e in events.read(campaign_id)
            if e.kind.startswith(family)
        ]

    def test_run_and_pause_announce_each_state_reached(
        self, campaigns, store, events
    ):
        # Three units on two lanes: one is still to queue after a run.
        create(store, lxd_max_jobs=2)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.pause(campaign_id=CAMPAIGN)
        campaigns.run(campaign_id=CAMPAIGN)

        assert self.kinds(events, "campaign.") == [
            "campaign.created",
            "campaign.running",
            "campaign.pausing",
            "campaign.running",
        ]

    def test_a_drain_is_announced_as_pausing_then_paused(
        self, campaigns, store, events, jobs
    ):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.pause(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()

        assert self.kinds(events, "campaign.") == [
            "campaign.created",
            "campaign.running",
            "campaign.pausing",
            "campaign.paused",
        ]

    def test_the_last_jobs_are_announced_as_finishing_then_complete(
        self, campaigns, store, events, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()

        assert self.kinds(events, "campaign.") == [
            "campaign.created",
            "campaign.running",
            "campaign.finishing",
            "campaign.complete",
        ]

    def test_a_state_event_says_what_caused_it(self, campaigns, store, events):
        create(store)
        campaigns.run(campaign_id=CAMPAIGN)

        (running,) = events.read(CAMPAIGN, kinds=["campaign.running"])

        assert running.data["cause"] == "run"
        assert running.data["from"] == "created"

    def test_completion_is_announced_with_the_counts(
        self, campaigns, store, events, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()

        (done,) = events.read(CAMPAIGN, kinds=["campaign.complete"])

        assert done.data["counts"]["passed"] == 1
        assert done.data["cause"] == "scheduler"

    def test_a_kill_announces_each_interrupted_unit(
        self, campaigns, store, events
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)

        campaigns.pause(campaign_id=CAMPAIGN, kill=True)

        (interrupted,) = events.read(CAMPAIGN, kinds=["unit.interrupted"])
        assert interrupted.data["job_id"] == "job1"
        assert interrupted.data["reason"] == KILLED

    def test_a_queued_job_is_announced_with_its_id(
        self, campaigns, store, events
    ):
        create(store, lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)

        opened = events.read(CAMPAIGN, kinds=["job.started"])

        assert len(opened) == 1
        assert opened[0].data["job_id"] == "job1"
        assert opened[0].data["release"] == UNITS[0].release
        assert opened[0].data["install_from"] == "proposed"

    def test_a_passing_unit_is_announced(self, campaigns, store, events, jobs):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()

        assert "unit.passed" in self.kinds(events)

    def test_a_failing_unit_carries_its_failing_steps(
        self, campaigns, store, events, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish(
            "job1",
            outcome="failed",
            failures=[
                {
                    "step": "Then it works",
                    "status": "failed",
                    "error_message": "it did not",
                }
            ],
        )
        campaigns.reconcile()

        (failed,) = events.read(CAMPAIGN, kinds=["unit.failed"])

        assert failed.data["failures"][0]["step"] == "Then it works"
        assert failed.data["failures"][0]["error_message"] == "it did not"

    def test_an_error_is_announced_with_its_reason(
        self, campaigns, store, events, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1", outcome="error", reason="the command exited 2")
        campaigns.reconcile()

        (errored,) = events.read(CAMPAIGN, kinds=["unit.errored"])

        assert errored.data["reason"] == "the command exited 2"


class TestRetryUnits:
    """Nothing re-runs a failure on its own; this is the only way."""

    @staticmethod
    def failed_once(campaigns, store, jobs, units=None):
        create(store, units=units or [UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1", outcome="failed")
        campaigns.reconcile()

    def test_retrying_a_complete_campaign_runs_it_again(
        self, campaigns, store, jobs
    ):
        self.failed_once(campaigns, store, jobs)
        assert state(store) == CampaignState.COMPLETE

        response = campaigns.retry_units(campaign_id=CAMPAIGN)

        # Its one unit is queued at once, so it is already finishing.
        assert response.retried == 1
        assert response.state == CampaignState.FINISHING
        assert jobs.queued_units == [UNITS[0], UNITS[0]]

    def test_retrying_a_paused_campaign_sets_intent_to_run(
        self, campaigns, store, jobs
    ):
        self.failed_once(campaigns, store, jobs, units=[UNITS[0], UNITS[1]])
        jobs.finish("job2")
        campaigns.reconcile()
        campaigns.pause(campaign_id=CAMPAIGN)

        response = campaigns.retry_units(campaign_id=CAMPAIGN)

        assert response.intent == Intent.RUN
        assert written_intents(store) == [Intent.RUN, Intent.HOLD, Intent.RUN]

    def test_a_retry_can_turn_a_failure_into_a_pass(
        self, campaigns, store, jobs
    ):
        self.failed_once(campaigns, store, jobs)
        campaigns.retry_units(campaign_id=CAMPAIGN)
        jobs.finish("job2")
        campaigns.reconcile()

        (status,) = reduce_units(store.read_records(CAMPAIGN))

        assert status.state == "passed"
        assert not status.retry_pending

    def test_the_request_is_recorded_with_its_reason(
        self, campaigns, store, jobs
    ):
        self.failed_once(campaigns, store, jobs)

        campaigns.retry_units(campaign_id=CAMPAIGN, reason="looked flaky")

        assert records_of(store, RetryRecord)[-1].reason == "looked flaky"

    def test_it_defaults_to_the_problem_states(self, campaigns, store, jobs):
        create(store, lxd_max_jobs=3)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1", outcome="failed")
        jobs.finish("job2")
        jobs.finish("job3", outcome="failed")
        campaigns.reconcile()

        response = campaigns.retry_units(campaign_id=CAMPAIGN)

        # The passing unit is left alone.
        assert response.retried == 2
        assert all(u.state == "failed" for u in response.units)

    def test_a_passing_unit_can_be_retried_when_named(
        self, campaigns, store, jobs
    ):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)
        jobs.finish("job1")
        campaigns.reconcile()

        response = campaigns.retry_units(
            campaign_id=CAMPAIGN, filters=Filters(state=("passed",))
        )

        assert response.retried == 1

    def test_filters_narrow_the_selection(self, campaigns, store, jobs):
        create(store, lxd_max_jobs=3)
        campaigns.run(campaign_id=CAMPAIGN)
        for job in ("job1", "job2", "job3"):
            jobs.finish(job, outcome="failed")
        campaigns.reconcile()

        response = campaigns.retry_units(
            campaign_id=CAMPAIGN, filters=Filters(release=("jammy",))
        )

        assert {u.release for u in response.units} == {"jammy"}

    def test_a_unit_in_flight_is_never_selected(self, campaigns, store):
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        campaigns.run(campaign_id=CAMPAIGN)

        with pytest.raises(CampaignError):
            campaigns.retry_units(
                campaign_id=CAMPAIGN,
                filters=Filters(state=(UnitState.IN_FLIGHT,)),
            )

    def test_matching_nothing_is_rejected(self, campaigns, store):
        create(store)
        campaigns.run(campaign_id=CAMPAIGN)

        with pytest.raises(CampaignError, match="nothing was re-queued"):
            campaigns.retry_units(campaign_id=CAMPAIGN)

    def test_retrying_is_announced(self, campaigns, store, jobs, events):
        self.failed_once(campaigns, store, jobs)

        campaigns.retry_units(campaign_id=CAMPAIGN, reason="flaky")

        (retried,) = events.read(CAMPAIGN, kinds=["unit.retried"])
        assert retried.data["previous_state"] == "failed"
        assert retried.data["reason"] == "flaky"


class TestSignals:
    """Derived signals reach the record log, and are said once."""

    def test_a_hung_job_is_reported_once(self, store, events):
        jobs = FakeJobRunner(started_at=AT)
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        make_campaigns(store, jobs, overdue_seconds=60).run(
            campaign_id=CAMPAIGN
        )

        later = make_campaigns(
            store, jobs, overdue_seconds=60, now=lambda: AN_HOUR_LATER
        )
        later.reconcile()
        later.reconcile()

        (overdue,) = events.read(CAMPAIGN, kinds=["job.overdue"])
        assert overdue.data["job_id"] == "job1"
        assert overdue.data["elapsed_seconds"] == 3600

    def test_time_spent_queued_is_not_overdue(self, store, events):
        # The job has not started: it has no start time.
        jobs = FakeJobRunner(started_at="")
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        make_campaigns(store, jobs, overdue_seconds=60).run(
            campaign_id=CAMPAIGN
        )

        make_campaigns(
            store, jobs, overdue_seconds=60, now=lambda: AN_HOUR_LATER
        ).reconcile()

        assert events.read(CAMPAIGN, kinds=["job.overdue"]) == []

    def test_a_restart_does_not_repeat_an_overdue_job(self, store, events):
        jobs = FakeJobRunner(started_at=AT)
        create(store, units=[UNITS[0]], lxd_max_jobs=1)
        make_campaigns(store, jobs, overdue_seconds=60).run(
            campaign_id=CAMPAIGN
        )

        for now in (AN_HOUR_LATER, TWO_HOURS_LATER):
            make_campaigns(
                store, jobs, overdue_seconds=60, now=lambda: now
            ).reconcile()

        assert len(events.read(CAMPAIGN, kinds=["job.overdue"])) == 1

    def test_starvation_is_reported_once_per_episode(self, store, events):
        jobs = FakeJobRunner(full_pools={Pool.LXD})
        campaigns = make_campaigns(store, jobs)
        create(store, lxd_max_jobs=1)

        def starved():
            return events.read(CAMPAIGN, kinds=["anomaly.starved"])

        campaigns.run(campaign_id=CAMPAIGN)
        campaigns.reconcile()
        campaigns.reconcile()
        assert len(starved()) == 1

        jobs.full_pools.clear()
        campaigns.reconcile()
        (_, queued_job, _, _) = jobs.queued[-1]
        jobs.finish(queued_job)
        jobs.full_pools.add(Pool.LXD)
        campaigns.reconcile()
        campaigns.reconcile()

        assert len(starved()) == 2

    def test_a_restart_does_not_repeat_starvation(self, store, events):
        jobs = FakeJobRunner(full_pools={Pool.LXD})
        create(store, lxd_max_jobs=1)
        make_campaigns(store, jobs).run(campaign_id=CAMPAIGN)

        make_campaigns(store, jobs).reconcile()

        assert len(events.read(CAMPAIGN, kinds=["anomaly.starved"])) == 1

    def test_a_scenario_failing_across_releases_is_reported(
        self, campaigns, store, jobs, events
    ):
        units = [
            Unit("features/a.feature", "A", release, "lxd-vm")
            for release in ("focal", "jammy", "noble")
        ]
        create(store, units=units, lxd_max_jobs=3)
        campaigns.run(campaign_id=CAMPAIGN)
        for job in ("job1", "job2", "job3"):
            jobs.finish(job, outcome="failed")
        campaigns.reconcile()

        reported = events.read(CAMPAIGN, kinds=["anomaly.*"])

        assert [e.kind for e in reported] == [
            "anomaly.repeated_scenario_failure"
        ]
        assert reported[0].data["releases"] == ["focal", "jammy", "noble"]

    def test_a_healthy_campaign_reports_no_anomalies(
        self, campaigns, store, jobs, events
    ):
        create(store, lxd_max_jobs=3)
        campaigns.run(campaign_id=CAMPAIGN)
        for job in ("job1", "job2", "job3"):
            jobs.finish(job)
        campaigns.reconcile()

        assert events.read(CAMPAIGN, kinds=["anomaly.*"]) == []


# Each named state, arranged on a two-unit campaign of units A and B.
IN_FLIGHT_JOB = "job-in-flight"


DONE_JOB = "job-done"


def _start(unit, job_id):
    return StartedRecord(unit=unit, job_id=job_id, install_from="local", at=AT)


def _tried(unit, job_id, outcome):
    return [
        _start(unit, job_id),
        FinishedRecord(unit=unit, job_id=job_id, outcome=outcome, at=AT),
    ]


A, B = UNITS[0], UNITS[1]


ARRANGED = {
    CampaignState.CREATED: [],
    CampaignState.RUNNING: [
        IntentRecord(Intent.RUN, Cause.RUN, AT),
        _start(A, IN_FLIGHT_JOB),
    ],
    CampaignState.FINISHING: [
        IntentRecord(Intent.RUN, Cause.RUN, AT),
        *_tried(A, DONE_JOB, "passed"),
        _start(B, IN_FLIGHT_JOB),
    ],
    CampaignState.COMPLETE: [
        IntentRecord(Intent.RUN, Cause.RUN, AT),
        *_tried(A, DONE_JOB, "passed"),
        *_tried(B, IN_FLIGHT_JOB, "failed"),
    ],
    CampaignState.PAUSING: [
        IntentRecord(Intent.HOLD, Cause.PAUSE, AT),
        _start(A, IN_FLIGHT_JOB),
    ],
    CampaignState.PAUSED: [
        IntentRecord(Intent.HOLD, Cause.PAUSE, AT),
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
    "run": lambda campaigns, campaign_id: campaigns.run(
        campaign_id=campaign_id
    ),
    "pause": lambda campaigns, campaign_id: campaigns.pause(
        campaign_id=campaign_id
    ),
    "pause-kill": lambda campaigns, campaign_id: campaigns.pause(
        campaign_id=campaign_id, kill=True
    ),
    "retry": lambda campaigns, campaign_id: campaigns.retry_units(
        campaign_id=campaign_id
    ),
}


# What each verb leaves each named state in. A verb that adds work queues
# it at once, and both units fit, so it lands in finishing. Retry with no
# filter selects the problem units, so it is refused where there are none.
CONTROL = {
    (CampaignState.CREATED, "run"): CampaignState.FINISHING,
    (CampaignState.CREATED, "pause"): CampaignState.PAUSED,
    (CampaignState.CREATED, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.CREATED, "retry"): Refused("no units matched"),
    (CampaignState.RUNNING, "run"): CampaignState.FINISHING,
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
    (CampaignState.COMPLETE, "retry"): CampaignState.FINISHING,
    (CampaignState.PAUSING, "run"): CampaignState.FINISHING,
    (CampaignState.PAUSING, "pause"): CampaignState.PAUSING,
    (CampaignState.PAUSING, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.PAUSING, "retry"): Refused("no units matched"),
    (CampaignState.PAUSED, "run"): CampaignState.FINISHING,
    (CampaignState.PAUSED, "pause"): CampaignState.PAUSED,
    (CampaignState.PAUSED, "pause-kill"): CampaignState.PAUSED,
    (CampaignState.PAUSED, "retry"): CampaignState.FINISHING,
}


def test_every_state_and_verb_has_an_expectation():
    assert set(CONTROL) == set(itertools.product(CampaignState, VERBS))


@pytest.mark.parametrize(
    "named, verb, expected",
    [(named, verb, want) for (named, verb), want in CONTROL.items()],
    ids=str,
)
def test_verbs_by_state(campaigns, store, named, verb, expected):
    campaign_id = create(store, units=[A, B])
    store.append(campaign_id, ARRANGED[named])

    if isinstance(expected, Refused):
        with pytest.raises(CampaignError, match=expected.reason):
            VERBS[verb](campaigns, campaign_id)
        assert state(store, campaign_id) == named
    else:
        VERBS[verb](campaigns, campaign_id)
        assert state(store, campaign_id) == expected
