"""Which queued jobs to launch, and where each one stands in its queue."""

from dataclasses import dataclass
from typing import Iterable, Mapping

from behave_mcp.domain.pools import Pool


@dataclass(frozen=True)
class QueuedJob:
    job_id: str
    pool: Pool
    queued_at: str


def _in_order(queued: Iterable[QueuedJob]) -> list[QueuedJob]:
    return sorted(queued, key=lambda job: (job.queued_at, job.job_id))


def plan_launches(
    queued: Iterable[QueuedJob],
    busy: Mapping[Pool, int],
    max_jobs: Mapping[Pool, int],
) -> list[str]:
    """The queued jobs to launch now, oldest first.

    Each pool fills its own free lanes from its own queue, so a full pool
    never holds back a job in another.
    """
    room = {
        pool: max(0, max_jobs.get(pool, 0) - busy.get(pool, 0))
        for pool in Pool
    }
    launches = []
    for job in _in_order(queued):
        if room[job.pool] > 0:
            room[job.pool] -= 1
            launches.append(job.job_id)
    return launches


def queue_position(job_id: str, queued: Iterable[QueuedJob]) -> int | None:
    """Where ``job_id`` stands in its pool's queue, 1 being next."""
    ordered = _in_order(queued)
    pool = next((job.pool for job in ordered if job.job_id == job_id), None)
    if pool is None:
        return None
    mine = [job.job_id for job in ordered if job.pool == pool]
    return mine.index(job_id) + 1
