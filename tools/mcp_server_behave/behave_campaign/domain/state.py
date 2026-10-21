"""What the records say now: unit states, intent, progress, named state."""

from __future__ import annotations

from typing import Iterable, Sequence

from behave_campaign.domain.records import (
    PROBLEM_OUTCOMES,
    STATES,
    Attempt,
    AttemptFinished,
    AttemptInterrupted,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    CampaignState,
    Intent,
    IntentRecord,
    Outcome,
    PlanRecord,
    Progress,
    Record,
    RetryRecord,
    StringEnum,
    Unit,
    UnitState,
    UnitStatus,
)
from behave_mcp.domain.pools import Pool


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


def reduce_units(records: Iterable[Record]) -> list[UnitStatus]:
    """Collapse records into one current status per planned unit.

    Attempts are assembled here: a ``started`` record opens one and the
    ``finished`` record sharing its ``job_id`` closes it, so one try is one
    attempt however many records described it.
    """
    planned: list[Unit] = []
    seen: set[Unit] = set()
    attempts: dict[Unit, list[Attempt]] = {}
    open_attempts: dict[tuple[Unit, str], int] = {}
    # A retry asks for another go; the next start answers it. Reading the
    # records in order is what decides which of the two came last.
    pending: dict[Unit, bool] = {}

    for record in records:
        if isinstance(record, (CampaignHeader, IntentRecord)):
            continue
        if isinstance(record, PlanRecord):
            if record.unit not in seen:
                seen.add(record.unit)
                planned.append(record.unit)
        elif isinstance(record, RetryRecord):
            pending[record.unit] = True
        elif isinstance(record, AttemptStarted):
            started = attempts.setdefault(record.unit, [])
            open_attempts[(record.unit, record.job_id)] = len(started)
            started.append(
                Attempt(
                    job_id=record.job_id,
                    install_from=record.install_from,
                    started_at=record.at,
                )
            )
            pending[record.unit] = False
        elif isinstance(record, AttemptFinished):
            index = open_attempts.pop((record.unit, record.job_id), None)
            if index is None:
                # A finish with no start: recorded out of band rather than
                # by a lane. Stand it up as a whole attempt of its own.
                attempts.setdefault(record.unit, []).append(
                    Attempt(
                        job_id=record.job_id,
                        started_at=record.at,
                        outcome=record.outcome,
                        finished_at=record.at,
                    )
                )
            else:
                opened = attempts[record.unit][index]
                attempts[record.unit][index] = Attempt(
                    job_id=opened.job_id,
                    install_from=opened.install_from,
                    started_at=opened.started_at,
                    outcome=record.outcome,
                    finished_at=record.at,
                )
            pending[record.unit] = False
        elif isinstance(record, AttemptInterrupted):
            index = open_attempts.pop((record.unit, record.job_id), None)
            if index is not None:
                opened = attempts[record.unit][index]
                attempts[record.unit][index] = Attempt(
                    job_id=opened.job_id,
                    install_from=opened.install_from,
                    started_at=opened.started_at,
                    finished_at=record.at,
                    interrupted=True,
                )

    statuses = []
    for unit in sorted(planned):
        unit_attempts = tuple(attempts.get(unit, ()))
        state, job_id = _unit_state(unit_attempts)
        statuses.append(
            UnitStatus(
                unit=unit,
                state=state,
                job_id=job_id,
                attempts=unit_attempts,
                retry_pending=pending.get(unit, False),
            )
        )
    return statuses


def busy_by_pool(statuses: Iterable[UnitStatus]) -> dict[Pool, int]:
    counts: dict[Pool, int] = {}
    for status in statuses:
        if status.in_flight:
            pool = status.unit.pool
            counts[pool] = counts.get(pool, 0) + 1
    return counts


def count_states(statuses: Iterable[UnitStatus]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    for status in statuses:
        counts[status.state] += 1
    return counts


def problems(statuses: Iterable[UnitStatus]) -> list[UnitStatus]:
    return [s for s in statuses if s.state in PROBLEM_OUTCOMES]


class GroupBy(StringEnum):
    """How a status report lists its problem units."""

    UNIT = "unit"
    SCENARIO = "scenario"


GROUPINGS = tuple(group.value for group in GroupBy)


def group_by_scenario(
    statuses: Iterable[UnitStatus],
) -> list[tuple[tuple[str, str], list[UnitStatus]]]:
    """Units bucketed by ``(feature, scenario)``, first seen first.

    One row per scenario is the shape a flaky-or-real judgement wants: the
    same scenario down on every release says defect, on one says flake.
    """
    grouped: dict[tuple[str, str], list[UnitStatus]] = {}
    for status in statuses:
        key = (status.unit.feature, status.unit.scenario)
        grouped.setdefault(key, []).append(status)
    return list(grouped.items())


def in_flight(statuses: Iterable[UnitStatus]) -> list[UnitStatus]:
    return [s for s in statuses if s.in_flight]


def intent(records: Iterable[Record]) -> Intent:
    """The latest intent written, or NEVER_RUN if none was."""
    current = Intent.NEVER_RUN
    for record in records:
        if isinstance(record, IntentRecord):
            current = record.intent
    return current


def progress(statuses: Sequence[UnitStatus]) -> Progress:
    """How much work is left, read off the units."""
    if statuses and not any(status.attempts for status in statuses):
        return Progress.UNTOUCHED
    if any(status.queueable for status in statuses):
        return Progress.OPEN
    if any(status.in_flight for status in statuses):
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


def state_of(records: Sequence[Record]) -> CampaignState:
    """The named state a campaign's records add up to."""
    statuses = reduce_units(records)
    return campaign_state(
        intent(records), progress(statuses), bool(in_flight(statuses))
    )
