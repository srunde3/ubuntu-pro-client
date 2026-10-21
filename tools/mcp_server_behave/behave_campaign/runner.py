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
from typing import Any, Callable, Sequence

from behave_campaign.domain.classify import Classification
from behave_campaign.domain.events import (
    Cause,
    EventKind,
    NewEvent,
    failure_details,
    state_events,
    unit_event_kind,
)
from behave_campaign.domain.records import (
    PROBLEM_OUTCOMES,
    AttemptInterrupted,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    CampaignState,
    Filters,
    Intent,
    IntentRecord,
    Outcome,
    Record,
    RetryRecord,
    Unit,
    UnitStatus,
)
from behave_campaign.domain.schedule import Lane, plan_tick
from behave_campaign.domain.signals import (
    DEFAULT_OVERDUE_SECONDS,
    Signal,
    detect_signals,
)
from behave_campaign.domain.state import (
    count_states,
    in_flight,
    intent,
    progress,
    reduce_units,
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
    EventLog,
    LaneRunner,
    LaneStartError,
    Ticker,
)
from behave_campaign.views.responses import campaign_summary, unit_view
from behave_mcp.domain.pools import Pool

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 2.0
# Why an attempt was interrupted: a pause with kill stopped its job.
KILLED_REASON = "paused_with_kill"
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
    """Runs, pauses and advances the one campaign that may be scheduling."""

    def __init__(
        self,
        *,
        store: CampaignStore,
        lanes: LaneRunner,
        lock: CampaignRunLock,
        events: EventLog,
        now: Callable[[], str],
        ticker: Ticker | None = None,
        overdue_seconds: float = DEFAULT_OVERDUE_SECONDS,
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

    def run(self, campaign_id: str) -> CampaignControlResponse:
        """Set intent to run, and schedule while there is work."""
        with self._guard:
            records = self._store.replay(campaign_id)
            self._check_not_blocked(campaign_id, records)
            written = self._intent_change(records, Intent.RUN)
            self._commit(campaign_id, records, written, Cause.RUN)
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
            written = self._intent_change(records, Intent.HOLD)
            self._store.append(campaign_id, written)
            interrupted: list[UnitStatus] = []
            unsignalled: list[str] = []
            if kill:
                interrupted = in_flight(reduce_units(records))
                unsignalled = self._kill(campaign_id, interrupted)
                written += self._interrupt(campaign_id, interrupted)
            self._announce(
                campaign_id, records, written, Cause.PAUSE, kill=kill
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
            retries: list[Record] = [
                RetryRecord(unit=status.unit, at=at, reason=reason)
                for status in selected
            ]
            written = [*retries, *self._intent_change(records, Intent.RUN)]
            self._commit(campaign_id, records, written, Cause.RETRY)
            self._events.append(
                campaign_id,
                [
                    NewEvent(
                        kind=EventKind.UNIT_RETRIED,
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

        plan = plan_tick(
            records=records,
            lanes=lanes,
            lane_limits=header.lanes,
            at=self._now(),
        )

        problems: list[str] = []
        emitted: list[NewEvent] = []
        written: list[Record] = []
        if plan.record:
            finished = [item.finished for item in plan.record]
            self._store.append(campaign_id, finished)
            written += finished
            problems.extend(
                item.problem for item in plan.record if item.problem
            )
            emitted.extend(self._outcome_events(plan.record, lanes))

        starved: list[Signal] = []
        started = self._open_lanes(
            campaign_id, header, plan.start, problems, starved
        )
        written += started
        emitted.extend(self._signal_events(campaign_id, starved))
        emitted.extend(
            NewEvent(
                kind=EventKind.LANE_STARTED,
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
        settled = [*records, *written]
        current = state_of(settled)
        emitted.extend(
            self._signal_events(
                campaign_id,
                detect_signals(
                    statuses=reduce_units(settled),
                    lanes=self._read_lane_ages(settled),
                    at=self._now(),
                    overdue_seconds=self._overdue_seconds,
                ),
            )
        )
        emitted.extend(self._state_events(records, settled, Cause.SCHEDULER))
        self._events.append(campaign_id, emitted)

        return TickReport(
            campaign_id=campaign_id,
            state=current,
            recorded=len(plan.record),
            started=len(started),
            lanes_busy=busy,
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
        self, records: Sequence[Record], wanted: Intent
    ) -> list[Record]:
        """The record that sets ``wanted``, or none if it is already set."""
        if intent(records) == wanted:
            return []
        return [IntentRecord(intent=wanted, at=self._now())]

    def _commit(
        self,
        campaign_id: str,
        before: Sequence[Record],
        written: Sequence[Record],
        cause: Cause,
    ) -> None:
        """Write a command's records, and schedule if there is work.

        The run lock is taken before anything is written, so a campaign
        another process is running is left exactly as it was.
        """
        after = [*before, *written]
        schedules = (
            state_of(after) not in IDLE_STATES and self._active != campaign_id
        )
        if schedules:
            self._lock.acquire(campaign_id)
        try:
            self._store.append(campaign_id, written)
        except Exception:
            if schedules:
                self._lock.release(campaign_id)
            raise
        self._announce(campaign_id, before, written, cause)
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

    def _interrupt(
        self, campaign_id: str, statuses: Sequence[UnitStatus]
    ) -> list[Record]:
        if not statuses:
            return []
        at = self._now()
        records: list[Record] = [
            AttemptInterrupted(
                unit=status.unit,
                job_id=status.job_id or "",
                at=at,
                reason=KILLED_REASON,
            )
            for status in statuses
        ]
        self._store.append(campaign_id, records)
        self._events.append(
            campaign_id,
            [
                NewEvent(
                    kind=EventKind.UNIT_INTERRUPTED,
                    at=at,
                    data={
                        **status.unit.as_dict(),
                        "job_id": status.job_id,
                        "reason": KILLED_REASON,
                    },
                )
                for status in statuses
            ],
        )
        return records

    def _announce(
        self,
        campaign_id: str,
        before: Sequence[Record],
        written: Sequence[Record],
        cause: Cause,
        **data: Any,
    ) -> None:
        self._events.append(
            campaign_id,
            self._state_events(before, [*before, *written], cause, **data),
        )

    def _state_events(
        self,
        before: Sequence[Record],
        after: Sequence[Record],
        cause: Cause,
        **data: Any,
    ) -> list[NewEvent]:
        statuses = reduce_units(after)
        return state_events(
            before=state_of(before),
            after=state_of(after),
            intent=intent(after),
            progress=progress(statuses),
            cause=cause,
            at=self._now(),
            data={**data, "counts": count_states(statuses)},
        )

    def _open_lanes(
        self,
        campaign_id: str,
        header: CampaignHeader,
        units: Sequence[Unit],
        problems: list[str],
        starved: list[Signal],
    ) -> list[AttemptStarted]:
        repo_root = Path(header.repo.root)
        install_from = header.install_from
        started: list[AttemptStarted] = []
        full: set[Pool] = set()
        for unit in units:
            if unit.pool in full:
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
                starved.append(
                    Signal(
                        kind=EventKind.ANOMALY_CAPACITY_STARVED,
                        key=unit.pool.value,
                        data={
                            **unit.as_dict(),
                            "pool": unit.pool.value,
                            "reason": str(error),
                        },
                    )
                )
                if error.full_pool is None:
                    break
                full.add(error.full_pool)
                continue
            self._reported.discard(
                (EventKind.ANOMALY_CAPACITY_STARVED, unit.pool.value)
            )
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
        self, campaign_id: str, signals: Sequence[Signal]
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
            for status in reduce_units(records)
            if status.in_flight and status.job_id
        ]

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

    def _outcome_events(
        self,
        classified: Sequence[Classification],
        lanes: Sequence[Lane],
    ) -> list[NewEvent]:
        """One outcome event per finished lane; the kind is the outcome."""
        results = {lane.unit: lane.result for lane in lanes if lane.finished}
        events: list[NewEvent] = []
        for item in classified:
            finished = item.finished
            body: dict[str, Any] = {
                **finished.unit.as_dict(),
                "job_id": finished.job_id,
            }
            if item.problem:
                body["problem"] = item.problem
            if finished.outcome == Outcome.FAILED:
                # Carried here so a caller can judge the failure without
                # going back for the job's report.
                body["failures"] = failure_details(results.get(finished.unit))
            events.append(
                NewEvent(
                    kind=unit_event_kind(finished.outcome, bool(item.problem)),
                    at=finished.at,
                    data=body,
                )
            )
        return events

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
