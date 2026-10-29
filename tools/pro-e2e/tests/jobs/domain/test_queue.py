"""Which queued jobs launch, in what order, and where each one stands."""

import pytest

from pro_e2e.jobs.domain.pools import Pool
from pro_e2e.jobs.domain.queue import QueuedJob, plan_launches, queue_position

EARLY = "2026-10-21T10:00:00.000000Z"
LATE = "2026-10-21T10:00:01.000000Z"

FIRST = QueuedJob("b-first", Pool.LXD, EARLY)
SECOND = QueuedJob("a-second", Pool.LXD, LATE)
# Queued at the same time as FIRST: the job id breaks the tie.
TIED = QueuedJob("c-tied", Pool.LXD, EARLY)
WSL_JOB = QueuedJob("w", Pool.WSL, LATE)

ONE_LXD = {Pool.LXD: 1, Pool.WSL: 1}


def test_the_oldest_job_launches_first():
    assert plan_launches([SECOND, FIRST], {}, ONE_LXD) == [FIRST.job_id]


def test_a_tie_on_time_goes_to_the_lower_job_id():
    assert plan_launches([TIED, FIRST], {}, ONE_LXD) == [FIRST.job_id]


def test_every_free_lane_is_filled():
    launches = plan_launches([FIRST, SECOND], {}, {Pool.LXD: 2})

    assert launches == [FIRST.job_id, SECOND.job_id]


def test_a_full_pool_does_not_hold_back_another():
    launches = plan_launches([FIRST, WSL_JOB], {Pool.LXD: 1}, ONE_LXD)

    assert launches == [WSL_JOB.job_id]


@pytest.mark.parametrize(
    "busy, max_jobs",
    [
        pytest.param({Pool.LXD: 1}, ONE_LXD, id="busy-at-max"),
        # Max jobs lowered below what is already running.
        pytest.param({Pool.LXD: 3}, ONE_LXD, id="busy-above-max"),
        pytest.param({}, {Pool.LXD: 0}, id="disabled"),
        pytest.param({}, {}, id="unset"),
    ],
)
def test_nothing_launches_without_room(busy, max_jobs):
    assert plan_launches([FIRST], busy, max_jobs) == []


def test_a_job_s_position_counts_only_its_own_pool():
    queued = [WSL_JOB, SECOND, FIRST]

    assert queue_position(FIRST.job_id, queued) == 1
    assert queue_position(SECOND.job_id, queued) == 2
    assert queue_position(WSL_JOB.job_id, queued) == 1


def test_a_job_not_queued_has_no_position():
    assert queue_position("gone", [FIRST]) is None
