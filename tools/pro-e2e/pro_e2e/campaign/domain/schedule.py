"""What one reconcile decides for a campaign: what to record, what to
queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from pro_e2e.campaign.domain.classify import JobStatus, classify_result
from pro_e2e.campaign.domain.records import (
    DEFAULT_INSTALL_FROM,
    CampaignState,
    FinishedRecord,
    Intent,
    InterruptedRecord,
    Record,
    StringEnum,
    Unit,
    UnitHistory,
    UnitState,
)
from pro_e2e.campaign.domain.state import replay
from pro_e2e.jobs.domain.pools import Pool


def select_schedulable(
    statuses: Sequence[UnitHistory], free: Mapping[Pool, int]
) -> list[UnitHistory]:
    """The units a campaign may queue, untouched ones first.

    Each pool fills only its own room, so a full pool never holds back
    units bound for another.
    """
    ready = [s for s in statuses if s.queueable]
    ready.sort(key=lambda s: s.state != UnitState.UNATTEMPTED)
    taken = {pool: 0 for pool in free}
    selected = []
    for status in ready:
        pool = status.unit.pool
        if taken.get(pool, 0) < free.get(pool, 0):
            taken[pool] += 1
            selected.append(status)
    return selected


class ControlAction(StringEnum):
    """What a caller can ask of a campaign. Pause takes an optional kill."""

    RUN = "run"
    PAUSE = "pause"


@dataclass(frozen=True)
class InFlightJob:
    """One unit's job, queued or running, and what the job service says of
    it now.

    ``install_from`` is carried from the attempt that queued it, so the
    attempt states what the job actually ran with. ``status`` is None until
    the job has been looked at.
    """

    unit: Unit
    job_id: str
    status: JobStatus | None = None
    install_from: str = DEFAULT_INSTALL_FROM

    @property
    def ended(self) -> bool:
        return self.status is not None and self.status.ended

    @property
    def started_at(self) -> str:
        """When the job began running; empty while it is queued."""
        return self.status.started_at if self.status is not None else ""


@dataclass(frozen=True)
class CampaignPlan:
    """What one reconcile decided for a campaign.

    ``record`` closes the attempts whose jobs ended; one whose result could
    not be classified carries its ``reason``. ``queue`` holds the units to
    queue jobs for. ``state`` is the campaign's named state once the ended
    jobs are recorded.
    """

    record: tuple[FinishedRecord | InterruptedRecord, ...] = ()
    queue: tuple[Unit, ...] = ()
    state: CampaignState = CampaignState.CREATED


def plan_campaign(
    *,
    records: Sequence[Record],
    jobs: Sequence[InFlightJob],
    max_jobs: Mapping[Pool, int],
    at: str = "",
) -> CampaignPlan:
    """Decide what to do next. Pure: queues nothing and writes nothing.

    Ended jobs are recorded first, so the units they free are queued in the
    same reconcile; an interrupted unit is queueable, so it is queued again
    as a new attempt. New jobs are queued only under intent run, which is
    what makes a paused campaign drain.
    """
    recorded = tuple(
        classify_result(job.unit, job.status, at)
        for job in jobs
        if job.status is not None and job.status.ended
    )

    replayed = replay([*records, *recorded])
    statuses = replayed.statuses()
    state = replayed.state()
    if replayed.intent != Intent.RUN or state == CampaignState.COMPLETE:
        return CampaignPlan(record=recorded, state=state)

    busy: dict[Pool, int] = {}
    for job in jobs:
        if not job.ended:
            busy[job.unit.pool] = busy.get(job.unit.pool, 0) + 1
    free = {
        pool: max(limit - busy.get(pool, 0), 0)
        for pool, limit in max_jobs.items()
    }
    # Only queueable units: a campaign never re-runs a problem unit on its
    # own. Every job in flight recorded an attempt when it was queued, so a
    # unit cannot be queued twice.
    return CampaignPlan(
        record=recorded,
        queue=tuple(
            status.unit for status in select_schedulable(statuses, free)
        ),
        state=state,
    )
