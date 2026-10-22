"""What the records say now: unit states, intent, progress, named state."""

from __future__ import annotations

from typing import Iterable, Sequence

from pro_behave.campaign.domain.records import (
    PROBLEM_OUTCOMES,
    STATES,
    Attempt,
    CampaignError,
    CampaignRecord,
    CampaignState,
    FinishedRecord,
    Intent,
    IntentRecord,
    InterruptedRecord,
    Outcome,
    OverdueRecord,
    PlanRecord,
    Progress,
    Record,
    RetryRecord,
    StartedRecord,
    StarvedRecord,
    StringEnum,
    Unit,
    UnitHistory,
    UnitState,
)
from pro_behave.jobs.domain.pools import Pool


def _deciding_attempt(attempts: Sequence[Attempt]) -> Attempt | None:
    """The attempt that decides a unit's outcome.

    A pass stands whatever came before or after it: a unit that has ever
    passed is passed. Otherwise the latest finished try is what counts. An
    interrupted try established nothing, so it never decides.
    """
    for attempt in attempts:
        if attempt.outcome == Outcome.PASSED:
            return attempt
    for attempt in reversed(attempts):
        if attempt.outcome is not None:
            return attempt
    return None


def _unit_state(attempts: Sequence[Attempt]) -> tuple[str, str | None]:
    """The unit's state and the job behind it."""
    for attempt in reversed(attempts):
        if attempt.in_flight:
            return UnitState.IN_FLIGHT, attempt.job_id
    deciding = _deciding_attempt(attempts)
    if deciding is None or deciding.outcome is None:
        return UnitState.UNATTEMPTED, None
    return UnitState(deciding.outcome), deciding.job_id


class Replay:
    """A campaign's records, applied one at a time.

    Keeps every planned unit's status current, and the totals the named
    state is read from, so a whole log replays in one pass however long it
    is. ``apply`` reports the unit whose status a record changed.

    Attempts are assembled here: a ``started`` record opens one and the
    ``finished`` or ``interrupted`` record sharing its ``job_id`` closes it,
    so one try is one attempt however many records described it.
    """

    def __init__(self) -> None:
        self.header: CampaignRecord | None = None
        self.intent = Intent.NEVER_RUN
        self._planned: dict[Unit, UnitHistory] = {}
        self._attempts: dict[Unit, list[Attempt]] = {}
        self._open: dict[tuple[Unit, str], int] = {}
        # A retry asks for another go; the next start answers it. Reading
        # the records in order is what decides which of the two came last.
        self._pending: dict[Unit, bool] = {}
        self._attempted = 0
        self._queueable = 0
        self._in_flight = 0
        self._counts = {state: 0 for state in STATES}
        # Starvation is reported once per episode: from a refusal until a
        # lane next opens in that pool. OverdueRecord is reported once per job.
        self.starved_pools: set[Pool] = set()
        self.overdue_jobs: set[str] = set()

    def apply(self, record: Record) -> UnitHistory | None:
        """Apply one record; return the new status of the unit it changed."""
        if isinstance(record, CampaignRecord):
            self.header = record
            return None
        if isinstance(record, IntentRecord):
            self.intent = record.intent
            return None
        if isinstance(record, PlanRecord):
            if record.unit in self._planned:
                return None
            self._planned[record.unit] = self._status_of(record.unit)
            self._count(self._planned[record.unit], 1)
            return self._planned[record.unit]
        if isinstance(record, StarvedRecord):
            self.starved_pools.add(record.pool)
            return None
        if isinstance(record, OverdueRecord):
            self.overdue_jobs.add(record.job_id)
            return None
        if isinstance(record, RetryRecord):
            self._pending[record.unit] = True
        elif isinstance(record, StartedRecord):
            self.starved_pools.discard(record.unit.pool)
            started = self._attempts.setdefault(record.unit, [])
            self._open[(record.unit, record.job_id)] = len(started)
            started.append(
                Attempt(
                    job_id=record.job_id,
                    install_from=record.install_from,
                    started_at=record.at,
                )
            )
            self._pending[record.unit] = False
        elif isinstance(record, FinishedRecord):
            self._close(
                record.unit,
                record.job_id,
                record.at,
                outcome=record.outcome,
            )
            self._pending[record.unit] = False
        elif isinstance(record, InterruptedRecord):
            if (record.unit, record.job_id) not in self._open:
                return None
            self._close(record.unit, record.job_id, record.at, outcome=None)
        else:
            return None
        return self._refresh(record.unit)

    def status(self, unit: Unit) -> UnitHistory | None:
        return self._planned.get(unit)

    def statuses(self) -> list[UnitHistory]:
        return [self._planned[unit] for unit in sorted(self._planned)]

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def progress(self) -> Progress:
        return _progress(
            units=len(self._planned),
            attempted=self._attempted,
            queueable=self._queueable,
            in_flight=self._in_flight,
        )

    def state(self) -> CampaignState:
        return campaign_state(
            self.intent, self.progress(), bool(self._in_flight)
        )

    def _close(
        self,
        unit: Unit,
        job_id: str,
        at: str,
        *,
        outcome: Outcome | None,
    ) -> None:
        index = self._open.pop((unit, job_id), None)
        if index is None:
            # A finish with no start: recorded out of band rather than by a
            # lane. Stand it up as a whole attempt of its own.
            self._attempts.setdefault(unit, []).append(
                Attempt(
                    job_id=job_id,
                    started_at=at,
                    outcome=outcome,
                    finished_at=at,
                )
            )
            return
        opened = self._attempts[unit][index]
        self._attempts[unit][index] = Attempt(
            job_id=opened.job_id,
            install_from=opened.install_from,
            started_at=opened.started_at,
            outcome=outcome,
            finished_at=at,
            interrupted=outcome is None,
        )

    def _refresh(self, unit: Unit) -> UnitHistory | None:
        old = self._planned.get(unit)
        if old is None:
            return None
        new = self._status_of(unit)
        self._count(old, -1)
        self._count(new, 1)
        self._planned[unit] = new
        return new

    def _status_of(self, unit: Unit) -> UnitHistory:
        attempts = tuple(self._attempts.get(unit, ()))
        state, job_id = _unit_state(attempts)
        return UnitHistory(
            unit=unit,
            state=state,
            job_id=job_id,
            attempts=attempts,
            retry_pending=self._pending.get(unit, False),
        )

    def _count(self, status: UnitHistory, sign: int) -> None:
        self._counts[status.state] += sign
        self._attempted += sign if status.attempts else 0
        self._queueable += sign if status.queueable else 0
        self._in_flight += sign if status.in_flight else 0


def replay(records: Iterable[Record]) -> Replay:
    replayed = Replay()
    for record in records:
        replayed.apply(record)
    return replayed


def reduce_units(records: Iterable[Record]) -> list[UnitHistory]:
    """One current status per planned unit, in unit order."""
    return replay(records).statuses()


def in_flight_by_pool(statuses: Iterable[UnitHistory]) -> dict[Pool, int]:
    counts: dict[Pool, int] = {}
    for status in statuses:
        if status.in_flight:
            pool = status.unit.pool
            counts[pool] = counts.get(pool, 0) + 1
    return counts


def count_states(statuses: Iterable[UnitHistory]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    for status in statuses:
        counts[status.state] += 1
    return counts


def problems(statuses: Iterable[UnitHistory]) -> list[UnitHistory]:
    return [s for s in statuses if s.state in PROBLEM_OUTCOMES]


class GroupBy(StringEnum):
    """How a status report lists its problem units."""

    UNIT = "unit"
    SCENARIO = "scenario"


GROUPINGS = tuple(group.value for group in GroupBy)


def group_by_scenario(
    statuses: Iterable[UnitHistory],
) -> list[tuple[tuple[str, str], list[UnitHistory]]]:
    """Units bucketed by ``(feature, scenario)``, first seen first.

    One row per scenario is the shape a flaky-or-real judgement wants: the
    same scenario down on every release says defect, on one says flake.
    """
    grouped: dict[tuple[str, str], list[UnitHistory]] = {}
    for status in statuses:
        key = (status.unit.feature, status.unit.scenario)
        grouped.setdefault(key, []).append(status)
    return list(grouped.items())


def in_flight(statuses: Iterable[UnitHistory]) -> list[UnitHistory]:
    return [s for s in statuses if s.in_flight]


def intent(records: Iterable[Record]) -> Intent:
    """The latest intent written, or NEVER_RUN if none was."""
    current = Intent.NEVER_RUN
    for record in records:
        if isinstance(record, IntentRecord):
            current = record.intent
    return current


def progress(statuses: Sequence[UnitHistory]) -> Progress:
    """How much work is left, read off the units."""
    return _progress(
        units=len(statuses),
        attempted=sum(1 for status in statuses if status.attempts),
        queueable=sum(1 for status in statuses if status.queueable),
        in_flight=sum(1 for status in statuses if status.in_flight),
    )


def _progress(
    *, units: int, attempted: int, queueable: int, in_flight: int
) -> Progress:
    if units and not attempted:
        return Progress.UNTOUCHED
    if queueable:
        return Progress.OPEN
    if in_flight:
        return Progress.FINISHING
    return Progress.SETTLED


# Keyed by intent, progress, and whether any job is in flight. Progress
# alone cannot tell a draining pause from a static one: a campaign on hold
# with queueable units left is open whether or not jobs are still running.
_STATE_OF = {
    (Intent.NEVER_RUN, Progress.UNTOUCHED, False): CampaignState.CREATED,
    (Intent.RUN, Progress.UNTOUCHED, False): CampaignState.RUNNING,
    (Intent.RUN, Progress.OPEN, False): CampaignState.RUNNING,
    (Intent.RUN, Progress.OPEN, True): CampaignState.RUNNING,
    (Intent.RUN, Progress.FINISHING, True): CampaignState.FINISHING,
    (Intent.RUN, Progress.SETTLED, False): CampaignState.COMPLETE,
    (Intent.HOLD, Progress.UNTOUCHED, False): CampaignState.PAUSED,
    (Intent.HOLD, Progress.OPEN, False): CampaignState.PAUSED,
    (Intent.HOLD, Progress.OPEN, True): CampaignState.PAUSING,
    (Intent.HOLD, Progress.FINISHING, True): CampaignState.PAUSING,
    (Intent.HOLD, Progress.SETTLED, False): CampaignState.COMPLETE,
}


def campaign_state(
    intent: Intent, progress: Progress, in_flight: bool
) -> CampaignState:
    """Name a campaign from its intent, progress and jobs in flight.

    Pairs missing from the table cannot arise from records this package
    writes, such as a campaign that never ran yet has attempts.
    """
    try:
        return _STATE_OF[(intent, progress, in_flight)]
    except KeyError:
        raise CampaignError(
            "no campaign state is {} and {}{}; its record is "
            "inconsistent".format(
                intent, progress, " with jobs in flight" if in_flight else ""
            )
        ) from None


def state_of(records: Iterable[Record]) -> CampaignState:
    """The named state a campaign's records add up to."""
    return replay(records).state()
