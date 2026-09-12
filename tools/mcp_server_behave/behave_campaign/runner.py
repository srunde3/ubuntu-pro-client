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
    AttemptRecord,
    CampaignError,
    Lane,
    NewEvent,
    Record,
    StateRecord,
    Unit,
)
from behave_campaign.messages import (
    CampaignControlResponse,
    StateCounts,
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

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 2.0
RESTART_REASON = "server_restart"


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
    ) -> None:
        self._store = store
        self._lanes = lanes
        self._lock = lock
        self._events = events
        self._now = now
        self._ticker = ticker if ticker is not None else ThreadTicker()
        self._guard = threading.Lock()
        self._active: str | None = None

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
            if state == domain.CANCELLED:
                raise CampaignError(
                    "campaign {!r} was cancelled; create a new one "
                    "instead".format(campaign_id)
                )
            if state == domain.COMPLETE:
                raise CampaignError(
                    "campaign {!r} has no work left".format(campaign_id)
                )
            if state == domain.RUNNING_STATE and self._active == campaign_id:
                return self._control_response(campaign_id)

            self._lock.acquire(campaign_id)
            try:
                self._append_state(campaign_id, domain.RUNNING_STATE)
            except Exception:
                self._lock.release(campaign_id)
                raise
            self._emit(campaign_id, domain.CAMPAIGN_STARTED)
            self._active = campaign_id
            self._start_ticker(campaign_id)
            return self._control_response(campaign_id)

    def pause(self, campaign_id: str) -> CampaignControlResponse:
        """Stop opening lanes. Jobs already in flight run to completion."""
        return self._transition(
            campaign_id,
            domain.PAUSED,
            allowed=(domain.RUNNING_STATE,),
        )

    def resume(self, campaign_id: str) -> CampaignControlResponse:
        """Reverse a pause, including the one a restart caused."""
        with self._guard:
            records = self._store.replay(campaign_id)
            state = domain.lifecycle(records)
            if state != domain.PAUSED:
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
            self._append_state(campaign_id, domain.RUNNING_STATE)
            self._emit(campaign_id, domain.CAMPAIGN_RESUMED)
            self._active = campaign_id
            self._start_ticker(campaign_id)
            return self._control_response(campaign_id)

    def cancel(self, campaign_id: str) -> CampaignControlResponse:
        """Close the campaign to further scheduling. In-flight lanes drain."""
        return self._transition(
            campaign_id,
            domain.CANCELLED,
            allowed=(domain.RUNNING_STATE, domain.PAUSED, domain.CREATED),
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
                campaign_id, [item.attempt for item in plan.record]
            )
            problems.extend(
                item.problem for item in plan.record if item.problem
            )
            emitted.extend(self._outcome_events(plan.record, lanes))

        started = self._open_lanes(campaign_id, header, plan.start, problems)
        emitted.extend(
            NewEvent(
                kind=domain.LANE_STARTED,
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
        lifecycle = domain.lifecycle(self._store.replay(campaign_id))
        if lifecycle == domain.COMPLETE:
            emitted.append(
                NewEvent(
                    kind=domain.CAMPAIGN_COMPLETE,
                    at=self._now(),
                    data={
                        "counts": domain.count_states(
                            domain.reduce_units(
                                self._store.replay(campaign_id)
                            )
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
            if domain.lifecycle(records) != domain.RUNNING_STATE:
                continue
            self._append_state(
                campaign_id, domain.PAUSED, reason=RESTART_REASON
            )
            self._emit(
                campaign_id,
                domain.CAMPAIGN_PAUSED,
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
        self, campaign_id: str, state: str, *, allowed: Sequence[str]
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
                    domain.CAMPAIGN_PAUSED
                    if state == domain.PAUSED
                    else domain.CAMPAIGN_CANCELLED
                ),
            )
            response = self._control_response(campaign_id)
            drained = state == domain.CANCELLED and not response.lanes_busy
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
    ) -> list[AttemptRecord]:
        repo_root = Path(header.repo.root)
        install_from = header.install_from
        started: list[AttemptRecord] = []
        for unit in units:
            try:
                job_id = self._lanes.start(
                    unit, repo_root=repo_root, install_from=install_from
                )
            except LaneStartError as error:
                # Capacity or a bad start: leave the unit unattempted so a
                # later tick picks it up, and say why.
                problems.append(str(error))
                break
            started.append(
                AttemptRecord(
                    unit=unit,
                    state="running",
                    job_id=job_id,
                    install_from=install_from,
                    at=self._now(),
                )
            )
        if started:
            # Recorded straight after starting, because a running attempt is
            # what keeps the unit out of the next tick's selection.
            self._store.append(campaign_id, started)
        return started

    def _outcome_events(
        self,
        classified: Sequence[domain.Classification],
        lanes: Sequence[Lane],
    ) -> list[NewEvent]:
        """A released-lane event and an outcome event per finished lane."""
        results = {lane.unit: lane.result for lane in lanes if lane.finished}
        events: list[NewEvent] = []
        for item in classified:
            attempt = item.attempt
            body: dict[str, Any] = {
                **attempt.unit.as_dict(),
                "job_id": attempt.job_id,
                "state": attempt.state,
            }
            events.append(
                NewEvent(
                    kind=domain.LANE_RELEASED, at=attempt.at, data=dict(body)
                )
            )
            if item.problem:
                body["problem"] = item.problem
            if attempt.state == "failed":
                # Carried here so a caller can judge the failure without
                # going back for the job's report.
                body["failures"] = domain.failure_details(
                    results.get(attempt.unit)
                )
            events.append(
                NewEvent(
                    kind=domain.unit_event_kind(
                        attempt.state, bool(item.problem)
                    ),
                    at=attempt.at,
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
            if status.state != "running" or not status.job_id:
                continue
            lanes.append(
                Lane(
                    unit=status.unit,
                    job_id=status.job_id,
                    result=self._lanes.poll(status.job_id),
                    # What this job actually ran with, from the running
                    # attempt the lane recorded when it opened.
                    install_from=status.attempts[-1].install_from,
                )
            )
        return lanes

    def _append_state(
        self, campaign_id: str, state: str, reason: str = ""
    ) -> None:
        self._store.append(
            campaign_id,
            [StateRecord(state=state, at=self._now(), reason=reason)],
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


def _is_finished(lifecycle: str, lanes_busy: int) -> bool:
    """A campaign is done with the scheduler once nothing is in flight."""
    return lifecycle in (domain.COMPLETE, domain.CANCELLED) and lanes_busy == 0


def _header_of(records: Sequence[Record]) -> domain.CampaignHeader:
    for record in records:
        if isinstance(record, domain.CampaignHeader):
            return record
    raise CampaignError("campaign has no header; it was never created")
