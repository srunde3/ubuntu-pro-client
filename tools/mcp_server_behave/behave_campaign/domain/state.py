"""What the records say now: unit states, lifecycle, remaining work."""

from __future__ import annotations

from typing import Iterable, Sequence

from behave_campaign.domain.records import (
    PROBLEM_OUTCOMES,
    STATES,
    Attempt,
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    Lifecycle,
    LifecycleRecord,
    Outcome,
    PlanRecord,
    Record,
    RetryRecord,
    StringEnum,
    Unit,
    UnitState,
    UnitStatus,
)
from behave_mcp.domain.pools import Pool


def _current_attempt(attempts: Sequence[Attempt]) -> Attempt | None:
    """The attempt that decides a unit's state.

    A pass stands whatever came before or after it: a unit that has ever
    passed is passed. Otherwise the latest try is what counts.
    """
    for attempt in attempts:
        if attempt.outcome == Outcome.PASSED:
            return attempt
    return attempts[-1] if attempts else None


def _unit_state(attempt: Attempt | None) -> str:
    if attempt is None:
        return UnitState.UNATTEMPTED
    return (
        UnitState(attempt.outcome)
        if not attempt.running
        else (UnitState.RUNNING)
    )


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
        if isinstance(record, (CampaignHeader, LifecycleRecord)):
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

    statuses = []
    for unit in sorted(planned):
        unit_attempts = tuple(attempts.get(unit, ()))
        current = _current_attempt(unit_attempts)
        statuses.append(
            UnitStatus(
                unit=unit,
                state=_unit_state(current),
                job_id=current.job_id if current else None,
                attempts=unit_attempts,
                retry_pending=pending.get(unit, False),
            )
        )
    return statuses


def select_next(
    statuses: Sequence[UnitStatus], limit: int
) -> list[UnitStatus]:
    """Never-attempted units first, then retryable problems."""
    if limit < 1:
        raise CampaignError("limit must be positive")
    unattempted = [s for s in statuses if s.state == UnitState.UNATTEMPTED]
    retryable = [s for s in statuses if s.state in PROBLEM_OUTCOMES]
    return (unattempted + retryable)[:limit]


def busy_by_pool(statuses: Iterable[UnitStatus]) -> dict[Pool, int]:
    counts: dict[Pool, int] = {}
    for status in statuses:
        if status.state == UnitState.RUNNING:
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


def running(statuses: Iterable[UnitStatus]) -> list[UnitStatus]:
    return [s for s in statuses if s.state == "running"]


def describe_units(units: Iterable[Unit], limit: int = 5) -> str:
    ordered = sorted(units)
    described = [
        " ".join(
            [unit.feature, unit.scenario, unit.release, unit.machine_type]
        )
        for unit in ordered[:limit]
    ]
    if len(ordered) > limit:
        described.append("... and {} more".format(len(ordered) - limit))
    return "; ".join(described)


def lifecycle(records: Sequence[Record]) -> str:
    """Return a campaign's current lifecycle state: the latest one written.

    Earlier records stay readable, so a campaign cancelled and reopened
    still reads as one that was closed and reopened.
    """
    current = Lifecycle.CREATED
    for record in records:
        if isinstance(record, LifecycleRecord):
            current = record.state
    return current


def outstanding(records: Sequence[Record]) -> list[UnitStatus]:
    """Units with work left: never attempted, in flight, or asked to retry.

    Problem units do not count, because one is only re-run when asked for.
    """
    return [
        status
        for status in reduce_units(records)
        if status.state in (UnitState.UNATTEMPTED, UnitState.RUNNING)
        or status.retry_pending
    ]


def last_state_reason(records: Sequence[Record]) -> str:
    """Return the reason on the most recent lifecycle transition."""
    reason = ""
    for record in records:
        if isinstance(record, LifecycleRecord):
            reason = record.reason
    return reason
