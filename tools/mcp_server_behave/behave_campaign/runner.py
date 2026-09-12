"""Drives campaigns: opens lanes, records what finishes, holds the lock.

This is the only module in the package that starts a thread. The scheduling
decisions it acts on are made by ``domain.plan_tick``, which is pure, so
everything interesting here can be tested by calling ``tick`` directly and
never starting the thread at all.

One campaign runs at a time. Its lane state is not held in memory: a unit in
flight is a unit whose latest attempt is ``running``, which is recorded
before the lane is released. That is what lets a tick after a restart pick
up exactly where the last one left off.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from behave_campaign import domain
from behave_campaign.domain import (
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    Filters,
    Lane,
    LifecycleRecord,
    NewEvent,
    Record,
    RetryRecord,
    Unit,
    UnitStatus,
)
from behave_campaign.messages import (
    CampaignControlResponse,
    ReopenCampaignResponse,
    RetryUnitsResponse,
    StateCounts,
    TickReport,
    UnitView,
)
from behave_campaign.ports import (
    CampaignRunLock,
    CampaignStore,
    EventLog,
    LaneRunner,
    LaneStartError,
    Ticker,
)

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 2.0
RESTART_REASON = "server_restart"
# Why a unit was recorded as errored while reopening a campaign: its job
# outlived the server that was watching it, so nothing will ever report it.
ABANDONED_REASON = "abandoned_on_reopen"


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
    """Starts, pauses and advances the one campaign that may be active."""

    def __init__(
        self,
        *,
        store: CampaignStore,
        lanes: LaneRunner,
        lock: CampaignRunLock,
        events: EventLog,
        now: Callable[[], str],
        ticker: Ticker | None = None,
        overdue_seconds: float = domain.DEFAULT_OVERDUE_SECONDS,
    ) -> None:
        self._store = store
        self._lanes = lanes
        self._lock = lock
        self._events = events
        self._now = now
        self._ticker = ticker if ticker is not None else ThreadTicker()
        self._overdue_seconds = overdue_seconds
        self._guard = threading.Lock()
        self._active: str | None = None
        # Signals already reported, so a condition that stays true is said
        # once rather than every tick. Per-process: a restart may repeat
        # one, which is better than losing it.
        self._reported: set[tuple[str, str]] = set()

    # -- control ----------------------------------------------------------

    def start(self, campaign_id: str) -> CampaignControlResponse:
        """Begin scheduling. Takes the run lock and starts the ticker."""
        with self._guard:
            if self._active and self._active != campaign_id:
                raise CampaignError(
                    "campaign {!r} is already running; only one campaign "
                    "runs at a time".format(self._active)
                )
            records = self._store.replay(campaign_id)
            state = domain.lifecycle(records)
            if state == domain.Lifecycle.CANCELLED:
                raise CampaignError(
                    "campaign {!r} was cancelled; reopen it first".format(
                        campaign_id
                    )
                )
            if state == domain.Lifecycle.COMPLETE:
                raise CampaignError(
                    "campaign {!r} has no work left".format(campaign_id)
                )
            if (
                state == domain.Lifecycle.RUNNING
                and self._active == campaign_id
            ):
                return self._control_response(campaign_id)

            self._lock.acquire(campaign_id)
            try:
                self._append_state(campaign_id, domain.Lifecycle.RUNNING)
            except Exception:
                self._lock.release(campaign_id)
                raise
            self._emit(campaign_id, domain.EventKind.CAMPAIGN_STARTED)
            self._active = campaign_id
            self._start_ticker(campaign_id)
            return self._control_response(campaign_id)

    def pause(self, campaign_id: str) -> CampaignControlResponse:
        """Stop opening lanes. Jobs already in flight run to completion."""
        return self._transition(
            campaign_id,
            domain.Lifecycle.PAUSED,
            allowed=(domain.Lifecycle.RUNNING,),
        )

    def resume(self, campaign_id: str) -> CampaignControlResponse:
        """Reverse a pause, including the one a restart caused."""
        with self._guard:
            records = self._store.replay(campaign_id)
            state = domain.lifecycle(records)
            if state != domain.Lifecycle.PAUSED:
                raise CampaignError(
                    "campaign {!r} is {}, not paused".format(
                        campaign_id, state
                    )
                )
            if self._active and self._active != campaign_id:
                raise CampaignError(
                    "campaign {!r} is already running; only one campaign "
                    "runs at a time".format(self._active)
                )
            if self._active != campaign_id:
                self._lock.acquire(campaign_id)
            self._append_state(campaign_id, domain.Lifecycle.RUNNING)
            self._emit(campaign_id, domain.EventKind.CAMPAIGN_RESUMED)
            self._active = campaign_id
            self._start_ticker(campaign_id)
            return self._control_response(campaign_id)

    def cancel(self, campaign_id: str) -> CampaignControlResponse:
        """Close the campaign to further scheduling. In-flight lanes drain.

        Allowed on a campaign that has already finished, because that is how
        one is closed against ``retry_units`` picking it up again. Use
        ``reopen`` to take that back.
        """
        return self._transition(
            campaign_id,
            domain.Lifecycle.CANCELLED,
            allowed=(
                domain.Lifecycle.RUNNING,
                domain.Lifecycle.PAUSED,
                domain.Lifecycle.CREATED,
                domain.Lifecycle.COMPLETE,
            ),
        )

    def reopen(
        self,
        campaign_id: str,
        *,
        reason: str = "",
        abandon_in_flight: bool = False,
    ) -> ReopenCampaignResponse:
        """Take a cancellation back, and put the campaign where it stood.

        The ``cancelled`` record stays where it is; this is appended after
        it, so a campaign closed by mistake still reads as one that was
        closed and reopened. A campaign with nothing left unattempted comes
        back ``complete`` rather than ``running`` -- reopening it is what
        lets ``retry_units`` reach its failed and skipped units again.

        Units left in flight by a cancel this process is no longer watching
        have to be dealt with first: ``abandon_in_flight`` records them as
        errored, which is the only honest reading of a job nothing will
        ever report on.
        """
        with self._guard:
            records = self._store.replay(campaign_id)
            state = domain.lifecycle(records)
            if state != domain.Lifecycle.CANCELLED:
                raise CampaignError(
                    "campaign {!r} is {}, not cancelled".format(
                        campaign_id, state
                    )
                )
            if self._active and self._active != campaign_id:
                raise CampaignError(
                    "campaign {!r} is already running; only one campaign "
                    "runs at a time".format(self._active)
                )

            # Lanes this process opened are still draining and will report
            # themselves. Anything else in flight belongs to a job nobody
            # is watching any more -- a cancel that outlived its server --
            # and those units would sit running for good.
            stranded: list[UnitStatus] = []
            if self._active != campaign_id:
                stranded = domain.running(domain.reduce_units(records))
            if stranded and not abandon_in_flight:
                raise CampaignError(
                    "campaign {!r} has {} unit(s) in flight that nothing is "
                    "watching: {}. Check those jobs, then reopen with "
                    "abandon_in_flight to record them as errored".format(
                        campaign_id,
                        len(stranded),
                        domain.describe_units(
                            status.unit for status in stranded
                        ),
                    )
                )

            at = self._now()
            abandoned = [
                AttemptFinished(
                    unit=status.unit,
                    job_id=status.job_id or "",
                    outcome=domain.Outcome.ERROR,
                    at=at,
                )
                for status in stranded
            ]
            reopened = LifecycleRecord(
                state=domain.Lifecycle.RUNNING, at=at, reason=reason
            )
            # Asked of the domain before anything is written, because the
            # lock is only worth taking for a campaign that will schedule.
            rescheduling = (
                domain.lifecycle([*records, *abandoned, reopened])
                == domain.Lifecycle.RUNNING
            )
            taking_lock = rescheduling and self._active != campaign_id
            if taking_lock:
                self._lock.acquire(campaign_id)
            try:
                self._store.append(campaign_id, [*abandoned, reopened])
            except Exception:
                if taking_lock:
                    self._lock.release(campaign_id)
                raise

            self._events.append(
                campaign_id,
                [
                    NewEvent(
                        kind=domain.EventKind.UNIT_ERRORED,
                        at=at,
                        data={
                            **record.unit.as_dict(),
                            "job_id": record.job_id,
                            "outcome": record.outcome,
                            "problem": ABANDONED_REASON,
                        },
                    )
                    for record in abandoned
                ],
            )
            self._emit(
                campaign_id,
                domain.EventKind.CAMPAIGN_REOPENED,
                reason=reason,
                abandoned=len(abandoned),
                rescheduling=rescheduling,
            )
            if rescheduling:
                self._active = campaign_id
                self._start_ticker(campaign_id)

            control = self._control_response(campaign_id)
            return ReopenCampaignResponse(
                **control.model_dump(),
                abandoned=[_unit_view(status) for status in stranded],
                rescheduling=rescheduling,
            )

    def retry_units(
        self,
        campaign_id: str,
        *,
        filters: Filters = Filters(),
        reason: str = "",
    ) -> RetryUnitsResponse:
        """Ask for another attempt at units that already had one.

        With no state filter this selects the problem states -- failed,
        skipped and errored -- because those are what a rerun is usually
        for. Name a state explicitly to retry something else, including a
        unit that passed. Units in flight are never selected; they are
        already being attempted.

        Re-queueing a campaign that had finished starts it scheduling
        again. A paused one accepts the request and stays paused.
        """
        with self._guard:
            records = self._store.replay(campaign_id)
            state = domain.lifecycle(records)
            if state == domain.Lifecycle.CANCELLED:
                raise CampaignError(
                    "campaign {!r} was cancelled; reopen it first".format(
                        campaign_id
                    )
                )

            selected = self._select_for_retry(records, filters)
            if not selected:
                raise CampaignError("no units matched; nothing was re-queued")

            at = self._now()
            self._store.append(
                campaign_id,
                [
                    RetryRecord(unit=status.unit, at=at, reason=reason)
                    for status in selected
                ],
            )
            self._events.append(
                campaign_id,
                [
                    NewEvent(
                        kind=domain.EventKind.UNIT_RETRIED,
                        at=at,
                        data={
                            **status.unit.as_dict(),
                            "previous_state": status.state,
                            "reason": reason,
                        },
                    )
                    for status in selected
                ],
            )

            lifecycle = domain.lifecycle(self._store.replay(campaign_id))
            rescheduling = lifecycle == domain.Lifecycle.RUNNING
            if rescheduling:
                # The ticker stops when a campaign finishes, and the lock
                # goes with it, so a retry on a completed campaign has to
                # take both again.
                self._lock.acquire(campaign_id)
                self._active = campaign_id
                self._start_ticker(campaign_id)

            return RetryUnitsResponse(
                campaign_id=campaign_id,
                requeued=len(selected),
                units=[_unit_view(status) for status in selected],
                lifecycle=lifecycle,
                rescheduling=rescheduling,
            )

    # -- the tick ---------------------------------------------------------

    def tick(self, campaign_id: str) -> TickReport:
        """Advance a campaign by one step. Safe to call directly in tests."""
        records = self._store.replay(campaign_id)
        header = _header_of(records)
        lanes = self._read_lanes(records)

        plan = domain.plan_tick(
            records=records,
            lanes=lanes,
            max_lanes=header.max_lanes,
            at=self._now(),
        )

        problems: list[str] = []
        emitted: list[NewEvent] = []
        if plan.record:
            self._store.append(
                campaign_id, [item.finished for item in plan.record]
            )
            problems.extend(
                item.problem for item in plan.record if item.problem
            )
            emitted.extend(self._outcome_events(plan.record, lanes))

        starved: list[NewEvent] = []
        started = self._open_lanes(
            campaign_id, header, plan.start, problems, starved
        )
        emitted.extend(starved)
        emitted.extend(
            NewEvent(
                kind=domain.EventKind.LANE_STARTED,
                at=attempt.at,
                data={
                    **attempt.unit.as_dict(),
                    "job_id": attempt.job_id,
                    "install_from": attempt.install_from,
                },
            )
            for attempt in started
        )

        busy = sum(1 for lane in lanes if not lane.finished) + len(started)
        settled = self._store.replay(campaign_id)
        lifecycle = domain.lifecycle(settled)
        emitted.extend(
            self._signal_events(
                campaign_id,
                domain.detect_signals(
                    statuses=domain.reduce_units(settled),
                    lanes=self._read_lane_ages(settled),
                    at=self._now(),
                    overdue_seconds=self._overdue_seconds,
                ),
            )
        )
        if lifecycle == domain.Lifecycle.COMPLETE:
            emitted.append(
                NewEvent(
                    kind=domain.EventKind.CAMPAIGN_COMPLETE,
                    at=self._now(),
                    data={
                        "counts": domain.count_states(
                            domain.reduce_units(settled)
                        )
                    },
                )
            )
        self._events.append(campaign_id, emitted)

        return TickReport(
            campaign_id=campaign_id,
            lifecycle=lifecycle,
            recorded=len(plan.record),
            started=len(started),
            lanes_busy=busy,
            problems=problems,
            finished=_is_finished(lifecycle, busy),
        )

    def recover(self) -> list[str]:
        """Pause any campaign left running by a server that went away.

        Lane state survives in the ledger, but whether those jobs are still
        alive is not something a fresh process can be sure of. Coming back
        paused puts that judgement where it belongs -- with whoever is
        watching -- rather than resuming into a half-known window.
        """
        paused = []
        for campaign_id in self._store.list_ids():
            try:
                records = self._store.replay(campaign_id)
            except CampaignError:
                logger.warning("skipping unreadable campaign %s", campaign_id)
                continue
            if domain.lifecycle(records) != domain.Lifecycle.RUNNING:
                continue
            self._append_state(
                campaign_id, domain.Lifecycle.PAUSED, reason=RESTART_REASON
            )
            self._emit(
                campaign_id,
                domain.EventKind.CAMPAIGN_PAUSED,
                reason=RESTART_REASON,
            )
            paused.append(campaign_id)
        return paused

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

    def _transition(
        self,
        campaign_id: str,
        state: domain.Lifecycle,
        *,
        allowed: Sequence[domain.Lifecycle],
    ) -> CampaignControlResponse:
        with self._guard:
            current = domain.lifecycle(self._store.replay(campaign_id))
            if current not in allowed:
                raise CampaignError(
                    "campaign {!r} is {}; cannot {}".format(
                        campaign_id, current, state
                    )
                )
            self._append_state(campaign_id, state)
            self._emit(
                campaign_id,
                (
                    domain.EventKind.CAMPAIGN_PAUSED
                    if state == domain.Lifecycle.PAUSED
                    else domain.EventKind.CAMPAIGN_CANCELLED
                ),
            )
            response = self._control_response(campaign_id)
            drained = (
                state == domain.Lifecycle.CANCELLED and not response.lanes_busy
            )
        # Joining the ticker happens outside the guard: a tick that is
        # already in flight must be able to finish without waiting on it.
        if drained:
            self._stop_ticker()
            self._release(campaign_id)
        return response

    def _open_lanes(
        self,
        campaign_id: str,
        header: domain.CampaignHeader,
        units: Sequence[Unit],
        problems: list[str],
        starved: list[NewEvent],
    ) -> list[AttemptStarted]:
        repo_root = Path(header.repo.root)
        install_from = header.install_from
        started: list[AttemptStarted] = []
        for unit in units:
            try:
                job_id = self._lanes.start(
                    unit, repo_root=repo_root, install_from=install_from
                )
            except LaneStartError as error:
                # Capacity or a bad start: leave the unit unattempted so a
                # later tick picks it up, and say why.
                problems.append(str(error))
                starved.append(
                    NewEvent(
                        kind=domain.EventKind.ANOMALY_CAPACITY_STARVED,
                        at=self._now(),
                        data={
                            **unit.as_dict(),
                            "reason": str(error),
                        },
                    )
                )
                break
            started.append(
                AttemptStarted(
                    unit=unit,
                    job_id=job_id,
                    install_from=install_from,
                    at=self._now(),
                )
            )
        if started:
            # Recorded straight after starting, because an unfinished
            # attempt is what keeps the unit out of the next tick's
            # selection.
            self._store.append(campaign_id, started)
        return started

    def _signal_events(
        self, campaign_id: str, signals: Sequence[domain.Signal]
    ) -> list[NewEvent]:
        """Turn new signals into events, skipping ones already reported."""
        fresh = []
        for signal in signals:
            marker = (signal.kind, signal.key)
            if marker in self._reported:
                continue
            self._reported.add(marker)
            fresh.append(
                NewEvent(
                    kind=signal.kind, at=self._now(), data=dict(signal.data)
                )
            )
        return fresh

    def _read_lane_ages(self, records: Sequence[Record]) -> list[Lane]:
        """Lanes as they stand now, without polling their jobs again."""
        return [
            Lane(
                unit=status.unit,
                job_id=status.job_id,
                install_from=status.attempts[-1].install_from,
                opened_at=status.attempts[-1].started_at,
            )
            for status in domain.reduce_units(records)
            if status.state == domain.UnitState.RUNNING and status.job_id
        ]

    @staticmethod
    def _select_for_retry(
        records: Sequence[Record], filters: Filters
    ) -> list[UnitStatus]:
        wanted = filters.state or domain.PROBLEM_OUTCOMES
        return [
            status
            for status in domain.reduce_units(records)
            # A unit in flight is already being attempted.
            if status.state != domain.UnitState.RUNNING
            and status.state in wanted
            and filters.matches_unit(status.unit)
        ]

    def _outcome_events(
        self,
        classified: Sequence[domain.Classification],
        lanes: Sequence[Lane],
    ) -> list[NewEvent]:
        """A released-lane event and an outcome event per finished lane."""
        results = {lane.unit: lane.result for lane in lanes if lane.finished}
        events: list[NewEvent] = []
        for item in classified:
            finished = item.finished
            body: dict[str, Any] = {
                **finished.unit.as_dict(),
                "job_id": finished.job_id,
                "outcome": finished.outcome,
            }
            events.append(
                NewEvent(
                    kind=domain.EventKind.LANE_RELEASED,
                    at=finished.at,
                    data=dict(body),
                )
            )
            if item.problem:
                body["problem"] = item.problem
            if finished.outcome == domain.Outcome.FAILED:
                # Carried here so a caller can judge the failure without
                # going back for the job's report.
                body["failures"] = domain.failure_details(
                    results.get(finished.unit)
                )
            events.append(
                NewEvent(
                    kind=domain.unit_event_kind(
                        finished.outcome, bool(item.problem)
                    ),
                    at=finished.at,
                    data=body,
                )
            )
        return events

    def _emit(self, campaign_id: str, kind: str, **data: Any) -> None:
        self._events.append(
            campaign_id, [NewEvent(kind=kind, at=self._now(), data=data)]
        )

    def _read_lanes(self, records: Sequence[Record]) -> list[Lane]:
        lanes = []
        for status in domain.reduce_units(records):
            if status.state != domain.UnitState.RUNNING or not status.job_id:
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

    def _append_state(
        self, campaign_id: str, state: domain.Lifecycle, reason: str = ""
    ) -> None:
        self._store.append(
            campaign_id,
            [LifecycleRecord(state=state, at=self._now(), reason=reason)],
        )

    def _control_response(self, campaign_id: str) -> CampaignControlResponse:
        records = self._store.replay(campaign_id)
        statuses = domain.reduce_units(records)
        return CampaignControlResponse(
            campaign_id=campaign_id,
            lifecycle=domain.lifecycle(records),
            reason=domain.last_state_reason(records),
            lanes_busy=len(domain.running(statuses)),
            counts=StateCounts(**domain.count_states(statuses)),
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


def _unit_view(status: UnitStatus) -> UnitView:
    return UnitView(
        **status.unit.as_dict(),
        state=status.state,
        job_id=status.job_id,
        attempt_count=len(status.attempts),
    )


def _is_finished(lifecycle: str, lanes_busy: int) -> bool:
    """A campaign is done with the scheduler once nothing is in flight."""
    return (
        lifecycle in (domain.Lifecycle.COMPLETE, domain.Lifecycle.CANCELLED)
        and lanes_busy == 0
    )


def _header_of(records: Sequence[Record]) -> domain.CampaignHeader:
    for record in records:
        if isinstance(record, domain.CampaignHeader):
            return record
    raise CampaignError("campaign has no header; it was never created")
