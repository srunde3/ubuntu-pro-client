"""What a scheduler tick decides: which lanes to record and to open."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from behave_campaign.domain.classify import Classification, classify_result
from behave_campaign.domain.records import (
    DEFAULT_INSTALL_SOURCE,
    Lifecycle,
    Record,
    StringEnum,
    Unit,
    UnitState,
    UnitStatus,
)
from behave_campaign.domain.state import lifecycle, outstanding, reduce_units
from behave_mcp.domain.pools import Pool


def is_schedulable(status: UnitStatus) -> bool:
    """Whether a scheduler may open a lane for this unit.

    Either it has never been attempted, or someone has asked for another go
    since its last attempt. A failed unit is not schedulable on its own:
    the scheduler runs unattended, and judging a failure flaky-or-real is
    for whoever is watching, not for the tick loop.
    """
    if status.state == UnitState.RUNNING:
        return False
    return status.state == UnitState.UNATTEMPTED or status.retry_pending


def select_schedulable(
    statuses: Sequence[UnitStatus], free: Mapping[Pool, int]
) -> list[UnitStatus]:
    """The units a scheduler may open lanes for, untouched ones first.

    Each pool fills only its own free lanes, so a full pool never holds
    back units bound for another.
    """
    ready = [s for s in statuses if is_schedulable(s)]
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
    """What a caller can ask a running server to do with a campaign."""

    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


@dataclass(frozen=True)
class Lane:
    """One unit occupying a lane, and its job's result if it has finished.

    ``install_from`` is carried from the running attempt this lane recorded
    when it opened, so the completed attempt states what the job actually
    ran with rather than what the campaign currently defaults to.
    """

    unit: Unit
    job_id: str
    result: Any = None
    install_from: str = DEFAULT_INSTALL_SOURCE
    # When the lane opened, from the running attempt that recorded it.
    opened_at: str = ""

    @property
    def finished(self) -> bool:
        return self.result is not None


@dataclass(frozen=True)
class TickPlan:
    """What one scheduler tick decided to do.

    ``record`` holds the finished lanes, classified. ``start`` holds the
    units to open new lanes for. Both are empty when there is nothing to do,
    which is the common case. ``complete`` means a running campaign has no
    work left and should be marked complete.
    """

    record: tuple[Classification, ...] = ()
    start: tuple[Unit, ...] = ()
    lifecycle: str = Lifecycle.CREATED
    complete: bool = False

    @property
    def idle(self) -> bool:
        return not self.record and not self.start


def plan_tick(
    *,
    records: Sequence[Record],
    lanes: Sequence[Lane],
    lane_limits: Mapping[Pool, int],
    at: str = "",
) -> TickPlan:
    """Decide what to do next. Pure: starts nothing and writes nothing.

    Finished lanes are classified first, so the units they free are
    available to fill in the same tick. New lanes are opened only while the
    campaign is running, which is what makes a paused campaign drain: its
    in-flight jobs are still recorded, and nothing new begins.
    """
    classified = tuple(
        classify_result(lane.unit, lane.result, at)
        for lane in lanes
        if lane.finished
    )

    after = [*records, *(item.finished for item in classified)]
    state = lifecycle(after)
    if state != Lifecycle.RUNNING:
        return TickPlan(record=classified, lifecycle=state)
    if not outstanding(after):
        return TickPlan(
            record=classified, lifecycle=Lifecycle.COMPLETE, complete=True
        )

    busy: dict[Pool, int] = {}
    for lane in lanes:
        if not lane.finished:
            busy[lane.unit.pool] = busy.get(lane.unit.pool, 0) + 1
    free = {
        pool: max(limit - busy.get(pool, 0), 0)
        for pool, limit in lane_limits.items()
    }
    if not any(free.values()):
        return TickPlan(record=classified, lifecycle=state)

    # Only units that are untouched or have an explicit retry pending: a
    # scheduler never re-runs a problem unit on its own. Running units are
    # excluded too, and every busy lane recorded a running attempt when it
    # opened, so a unit cannot enter two lanes.
    return TickPlan(
        record=classified,
        start=tuple(
            status.unit
            for status in select_schedulable(reduce_units(after), free)
        ),
        lifecycle=state,
    )
