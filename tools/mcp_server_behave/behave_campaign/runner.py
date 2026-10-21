"""Drives campaigns: opens lanes, records what finishes, holds the lock.

This is the only module in the package that starts a thread. The scheduling
decisions it acts on are made by ``schedule.plan_tick``, which is pure, so
everything interesting here can be tested by calling ``tick`` directly and
never starting the thread at all.

One campaign schedules at a time. Its lane state is not held in memory: a
unit in flight is a unit with an attempt that has neither finished nor been
interrupted, which is recorded before the lane is released. That is what
lets a tick after a restart pick up exactly where the last one left off.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Sequence

from behave_campaign.domain.classify import KILLED
from behave_campaign.domain.records import (
    PROBLEM_OUTCOMES,
    AttemptFinished,
    AttemptInterrupted,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    CampaignState,
    Cause,
    Filters,
    Intent,
    IntentRecord,
    Record,
    RetryRecord,
    Starved,
    Unit,
    UnitStatus,
)
from behave_campaign.domain.schedule import Lane, plan_tick
from behave_campaign.domain.signals import (
    DEFAULT_OVERDUE_SECONDS,
    overdue_lanes,
)
from behave_campaign.domain.state import (
    in_flight,
    intent,
    reduce_units,
    replay,
    state_of,
)
from behave_campaign.messages import (
    CampaignControlResponse,
    CampaignSummary,
    RetryUnitsResponse,
    TickReport,
)
from behave_campaign.ports import (
    CampaignRunLock,
    CampaignStore,
    LaneRunner,
    LaneStartError,
    Ticker,
)
from behave_campaign.views.responses import campaign_summary, unit_view
from behave_mcp.domain.pools import Pool

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 2.0
# States with nothing in flight and nothing to schedule: the ticker stops
# and the run lock is released.
IDLE_STATES = (CampaignState.PAUSED, CampaignState.COMPLETE)


class ThreadTicker:
    """A ``Ticker`` backed by a daemon thread.

    Deliberately empty of decisions: it ticks, it stops, and it survives a
    tick that raises. Everything about *what* a tick does lives elsewhere.
    """

    def __init__(
        self, interval: float = DEFAULT_TICK_INTERVAL_SECONDS
    ) -> None:
        self._interval = interval
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, tick: Callable[[], bool]) -> None:
        if self.is_running():
            return
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(tick,),
            name="campaign-ticker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # Never join the ticker from inside itself.
            if threading.current_thread() is not thread:
                thread.join(self._interval * 2)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, tick: Callable[[], bool]) -> None:
        # Ticks before waiting, so a campaign starts filling lanes at once
        # rather than idling for one interval.
        while True:
            try:
                if tick():
                    return
            except Exception:
                # A tick that blows up must not kill the loop: the next one
                # re-reads the campaign from disk and may well succeed.
                logger.exception("campaign tick failed")
            if self._stopped.wait(self._interval):
                return


class CampaignRunner:
    """Runs, pauses and advances the one campaign that may be scheduling.

    It writes records only. Everything a watcher hears is derived from
    them, so the runner keeps nothing about a campaign in memory but which
    one it is scheduling.
    """

    def __init__(
        self,
        *,
        store: CampaignStore,
        lanes: LaneRunner,
        lock: CampaignRunLock,
        now: Callable[[], str],
        ticker: Ticker | None = None,
        overdue_seconds: float = DEFAULT_OVERDUE_SECONDS,
    ) -> None:
        self._store = store
        self._lanes = lanes
        self._lock = lock
        self._now = now
        self._ticker = ticker if ticker is not None else ThreadTicker()
        self._overdue_seconds = overdue_seconds
        self._guard = threading.Lock()
        self._active: str | None = None

    # -- control ----------------------------------------------------------

    def run(self, campaign_id: str) -> CampaignControlResponse:
        """Set intent to run, and schedule while there is work."""
        with self._guard:
            records = self._store.replay(campaign_id)
            self._check_not_blocked(campaign_id, records)
            self._commit(
                campaign_id,
                records,
                self._intent_change(records, Intent.RUN, Cause.RUN),
            )
            return self._control_response(campaign_id)

    def pause(
        self, campaign_id: str, *, kill: bool = False
    ) -> CampaignControlResponse:
        """Set intent to hold. Jobs in flight finish, or are killed.

        A killed job's attempt is recorded as interrupted at once, so its
        unit is queueable again whatever the job does next.
        """
        with self._guard:
            records = self._store.replay(campaign_id)
            self._store.append(
                campaign_id,
                self._intent_change(records, Intent.HOLD, Cause.PAUSE),
            )
            interrupted: list[UnitStatus] = []
            unsignalled: list[str] = []
            if kill:
                interrupted = in_flight(reduce_units(records))
                unsignalled = self._kill(campaign_id, interrupted)
                self._store.append(
                    campaign_id, self._interruptions(interrupted)
                )
            response = self._control_response(campaign_id)
            response.interrupted = [unit_view(s) for s in interrupted]
            response.unsignalled = unsignalled
            idle = response.state in IDLE_STATES
        # Joining the ticker happens outside the guard: a tick that is
        # already in flight must be able to finish without waiting on it.
        if idle and self._active == campaign_id:
            self._stop_ticker()
            self._release(campaign_id)
        return response

    def retry_units(
        self,
        campaign_id: str,
        *,
        filters: Filters = Filters(),
        reason: str = "",
    ) -> RetryUnitsResponse:
        """Ask for another attempt at units that already had one, and run.

        With no state filter this selects the problem states -- failed,
        skipped and errored -- because those are what a rerun is usually
        for. Name a state explicitly to retry something else, including a
        unit that passed. Units in flight are never selected; they are
        already being attempted.
        """
        with self._guard:
            records = self._store.replay(campaign_id)
            selected = self._select_for_retry(records, filters)
            if not selected:
                raise CampaignError("no units matched; nothing was re-queued")
            self._check_not_blocked(campaign_id, records, retrying=True)

            at = self._now()
            # The intent comes first, so the retries are what move the
            # campaign, and its state changes once.
            self._commit(
                campaign_id,
                records,
                [
                    *self._intent_change(records, Intent.RUN, Cause.RETRY),
                    *(
                        RetryRecord(unit=status.unit, at=at, reason=reason)
                        for status in selected
                    ),
                ],
            )
            return RetryUnitsResponse(
                **self._summary(campaign_id).model_dump(),
                requeued=len(selected),
                units=[unit_view(status) for status in selected],
            )

    # -- the tick ---------------------------------------------------------

    def tick(self, campaign_id: str) -> TickReport:
        """Advance a campaign by one step. Safe to call directly in tests.

        Holds the same guard as the control verbs, so a pause or retry
        lands wholly before or wholly after a tick.
        """
        with self._guard:
            return self._tick_locked(campaign_id)

    def _tick_locked(self, campaign_id: str) -> TickReport:
        records = self._store.replay(campaign_id)
        header = _header_of(records)
        lanes = self._read_lanes(records)
        at = self._now()

        plan = plan_tick(
            records=records, lanes=lanes, lane_limits=header.lanes, at=at
        )
        self._store.append(campaign_id, plan.record)
        problems = [
            item.problem
            for item in plan.record
            if isinstance(item, AttemptFinished) and item.problem
        ]

        started, refused = self._open_lanes(
            campaign_id, header, plan.start, problems
        )
        replayed = replay([*records, *plan.record, *started])
        observed: list[Record] = [
            refusal
            for refusal in refused
            if refusal.pool not in replayed.starved_pools
        ]
        observed += overdue_lanes(
            [lane for lane in lanes if not lane.finished],
            at=at,
            overdue_seconds=self._overdue_seconds,
            reported=replayed.overdue_jobs,
        )
        self._store.append(campaign_id, observed)

        current = replayed.state()
        return TickReport(
            campaign_id=campaign_id,
            state=current,
            recorded=len(plan.record),
            started=len(started),
            lanes_busy=replayed.in_flight,
            problems=problems,
            finished=current in IDLE_STATES,
        )

    def active_campaign(self) -> str | None:
        return self._active

    def shutdown(self) -> None:
        """Stop the ticker without changing the campaign's state."""
        active = self._active
        self._stop_ticker()
        if active:
            self._lock.release(active)
        self._active = None

    # -- internals --------------------------------------------------------

    def _check_not_blocked(
        self,
        campaign_id: str,
        records: Sequence[Record],
        retrying: bool = False,
    ) -> None:
        """Refuse to schedule while another campaign holds the scheduler.

        Running a complete campaign schedules nothing, so it is allowed;
        retrying one always adds work.
        """
        if not self._active or self._active == campaign_id:
            return
        if not retrying and state_of(records) == CampaignState.COMPLETE:
            return
        raise CampaignError(
            "campaign {!r} is scheduling; only one campaign schedules at a "
            "time. Pause it first.".format(self._active)
        )

    def _intent_change(
        self, records: Sequence[Record], wanted: Intent, cause: Cause
    ) -> list[Record]:
        """The record that sets ``wanted``, or none if it is already set."""
        if intent(records) == wanted:
            return []
        return [IntentRecord(intent=wanted, cause=cause, at=self._now())]

    def _commit(
        self,
        campaign_id: str,
        before: Sequence[Record],
        written: Sequence[Record],
    ) -> None:
        """Write a command's records, and schedule if there is work.

        The run lock is taken before anything is written, so a campaign
        another process is running is left exactly as it was.
        """
        schedules = (
            state_of([*before, *written]) not in IDLE_STATES
            and self._active != campaign_id
        )
        if schedules:
            self._lock.acquire(campaign_id)
        try:
            self._store.append(campaign_id, written)
        except Exception:
            if schedules:
                self._lock.release(campaign_id)
            raise
        if schedules:
            self._active = campaign_id
            self._start_ticker(campaign_id)

    def _kill(
        self, campaign_id: str, statuses: Sequence[UnitStatus]
    ) -> list[str]:
        """Signal each job; return the ids of those that could not be."""
        unsignalled = []
        for status in statuses:
            job_id = status.job_id or ""
            if not self._lanes.kill(job_id):
                logger.warning(
                    "campaign %s: job %s could not be signalled",
                    campaign_id,
                    job_id,
                )
                unsignalled.append(job_id)
        return unsignalled

    def _interruptions(
        self, statuses: Sequence[UnitStatus]
    ) -> list[AttemptInterrupted]:
        at = self._now()
        return [
            AttemptInterrupted(
                unit=status.unit,
                job_id=status.job_id or "",
                at=at,
                reason=KILLED,
            )
            for status in statuses
        ]

    def _open_lanes(
        self,
        campaign_id: str,
        header: CampaignHeader,
        units: Sequence[Unit],
        problems: list[str],
    ) -> tuple[list[AttemptStarted], list[Starved]]:
        """Start a job for each unit; return what started and what was
        refused, one refusal per pool."""
        repo_root = Path(header.repo.root)
        install_from = header.install_from
        started: list[AttemptStarted] = []
        refused: dict[Pool, Starved] = {}
        for unit in units:
            if unit.pool in refused:
                continue
            try:
                job_id = self._lanes.start(
                    unit, repo_root=repo_root, install_from=install_from
                )
            except LaneStartError as error:
                # Capacity or a bad start: leave the unit unattempted so a
                # later tick picks it up, and say why. A full pool only
                # stops that pool; anything else stops the tick.
                problems.append(str(error))
                refused[unit.pool] = Starved(
                    unit=unit,
                    pool=unit.pool,
                    reason=str(error),
                    at=self._now(),
                )
                if error.full_pool is None:
                    break
                continue
            started.append(
                AttemptStarted(
                    unit=unit,
                    job_id=job_id,
                    install_from=install_from,
                    at=self._now(),
                )
            )
        # Recorded straight after starting, because an unfinished attempt is
        # what keeps the unit out of the next tick's selection.
        self._store.append(campaign_id, started)
        return started, list(refused.values())

    @staticmethod
    def _select_for_retry(
        records: Sequence[Record], filters: Filters
    ) -> list[UnitStatus]:
        wanted = filters.state or PROBLEM_OUTCOMES
        return [
            status
            for status in reduce_units(records)
            # A unit in flight is already being attempted.
            if not status.in_flight
            and status.state in wanted
            and filters.matches_unit(status.unit)
        ]

    def _read_lanes(self, records: Sequence[Record]) -> list[Lane]:
        lanes = []
        for status in reduce_units(records):
            if not status.in_flight or not status.job_id:
                continue
            open_attempt = status.attempts[-1]
            lanes.append(
                Lane(
                    unit=status.unit,
                    job_id=status.job_id,
                    result=self._lanes.poll(status.job_id),
                    # What this job actually ran with, and when it began.
                    install_from=open_attempt.install_from,
                    opened_at=open_attempt.started_at,
                )
            )
        return lanes

    def _summary(self, campaign_id: str) -> CampaignSummary:
        return campaign_summary(campaign_id, self._store.replay(campaign_id))

    def _control_response(self, campaign_id: str) -> CampaignControlResponse:
        return CampaignControlResponse(
            **self._summary(campaign_id).model_dump()
        )

    def _start_ticker(self, campaign_id: str) -> None:
        self._ticker.start(lambda: self._tick_once(campaign_id))

    def _tick_once(self, campaign_id: str) -> bool:
        """One tick from the ticker's side: release everything when done."""
        report = self.tick(campaign_id)
        if report.finished:
            self._release(campaign_id)
        return report.finished

    def _stop_ticker(self) -> None:
        self._ticker.stop()

    def _release(self, campaign_id: str) -> None:
        self._lock.release(campaign_id)
        if self._active == campaign_id:
            self._active = None


def _header_of(records: Sequence[Record]) -> CampaignHeader:
    for record in records:
        if isinstance(record, CampaignHeader):
            return record
    raise CampaignError("campaign has no header; it was never created")
