"""The events a campaign announces, derived from its record log.

Nothing is stored here. Every event follows from a record, and an event's
``seq`` is its position in the derived feed, so replaying the same log
always numbers the same events the same way, across restarts and between
processes.
"""

from __future__ import annotations

from typing import Any, Sequence

from behave_campaign.domain.events import (
    STATE_EVENT,
    Event,
    EventKind,
    unit_event_kind,
)
from behave_campaign.domain.records import (
    AttemptFinished,
    AttemptInterrupted,
    AttemptStarted,
    CampaignHeader,
    Cause,
    IntentRecord,
    Outcome,
    Overdue,
    PlanRecord,
    Record,
    RetryRecord,
    Starved,
    UnitState,
    UnitStatus,
    lanes_as_dict,
)
from behave_campaign.domain.signals import (
    REPEATED_FAILURE_RELEASES,
    REPEATED_SKIP_UNITS,
)
from behave_campaign.domain.state import Replay


def derive_feed(campaign_id: str, records: Sequence[Record]) -> list[Event]:
    """Every event the campaign's records announce, oldest first."""
    return _Feed(campaign_id).derive(records)


class _Feed:
    def __init__(self, campaign_id: str) -> None:
        self._campaign_id = campaign_id
        self._replay = Replay()
        self._events: list[Event] = []
        # The derived signals, so each is announced once.
        self._failing: dict[tuple[str, str], set[str]] = {}
        self._skipped = 0
        self._reported: set[str] = set()

    def derive(self, records: Sequence[Record]) -> list[Event]:
        # The header and the plan come first, written together at creation;
        # the campaign has a state only once every unit is planned.
        head = 0
        while head < len(records) and isinstance(
            records[head], (CampaignHeader, PlanRecord)
        ):
            self._replay.apply(records[head])
            head += 1
        header = self._replay.header
        if header is not None:
            self._created(header)
        for record in records[head:]:
            self._apply(record)
        return self._events

    def _created(self, header: CampaignHeader) -> None:
        self._emit(
            EventKind.CAMPAIGN_CREATED,
            header.at,
            {
                "total_units": len(self._replay.statuses()),
                "install_from": header.install_from,
                "lanes": lanes_as_dict(header.lanes),
                "scope": header.filters.scope_as_dict(),
            },
        )

    def _apply(self, record: Record) -> None:
        before = self._replay.state()
        unit = getattr(record, "unit", None)
        previous = self._replay.status(unit) if unit is not None else None
        changed = self._replay.apply(record)
        self._announce(record, previous)
        if changed is not None:
            self._signals(previous, changed)
        after = self._replay.state()
        if after != before:
            self._emit(
                STATE_EVENT[after],
                getattr(record, "at", ""),
                {
                    "from": before.value,
                    "intent": self._replay.intent.value,
                    "progress": self._replay.progress().value,
                    "cause": (
                        record.cause
                        if isinstance(record, IntentRecord)
                        else (
                            Cause.RETRY
                            if isinstance(record, RetryRecord)
                            else Cause.SCHEDULER
                        )
                    ).value,
                    "counts": self._replay.counts(),
                },
            )

    def _announce(self, record: Record, previous: UnitStatus | None) -> None:
        """The event a record stands for, if it stands for one."""
        if isinstance(record, AttemptStarted):
            self._emit(
                EventKind.LANE_STARTED,
                record.at,
                {
                    **record.unit.as_dict(),
                    "job_id": record.job_id,
                    "install_from": record.install_from,
                },
            )
        elif isinstance(record, AttemptFinished):
            data: dict[str, Any] = {
                **record.unit.as_dict(),
                "job_id": record.job_id,
            }
            if record.problem:
                data["problem"] = record.problem
            if record.outcome == Outcome.FAILED:
                data["failures"] = [f.as_dict() for f in record.failures]
            self._emit(
                unit_event_kind(record.outcome, bool(record.problem)),
                record.at,
                data,
            )
        elif isinstance(record, AttemptInterrupted):
            self._emit(
                EventKind.UNIT_INTERRUPTED,
                record.at,
                {
                    **record.unit.as_dict(),
                    "job_id": record.job_id,
                    "reason": record.reason,
                },
            )
        elif isinstance(record, RetryRecord):
            self._emit(
                EventKind.UNIT_RETRIED,
                record.at,
                {
                    **record.unit.as_dict(),
                    "previous_state": (
                        previous.state if previous is not None else ""
                    ),
                    "reason": record.reason,
                },
            )
        elif isinstance(record, Starved):
            self._emit(
                EventKind.ANOMALY_CAPACITY_STARVED,
                record.at,
                {
                    **record.unit.as_dict(),
                    "pool": record.pool.value,
                    "reason": record.reason,
                },
            )
        elif isinstance(record, Overdue):
            self._emit(
                EventKind.LANE_OVERDUE,
                record.at,
                {
                    **record.unit.as_dict(),
                    "job_id": record.job_id,
                    "elapsed_seconds": record.elapsed_seconds,
                },
            )

    def _signals(self, previous: UnitStatus | None, now: UnitStatus) -> None:
        """Announce a derived signal the first time its condition holds."""
        scenario = (now.unit.feature, now.unit.scenario)
        releases = self._failing.setdefault(scenario, set())
        if previous is not None and previous.state == UnitState.FAILED:
            releases.discard(now.unit.release)
        if now.state == UnitState.FAILED:
            releases.add(now.unit.release)
        if previous is not None and previous.state == UnitState.SKIPPED:
            self._skipped -= 1
        if now.state == UnitState.SKIPPED:
            self._skipped += 1

        at = now.attempts[-1].finished_at if now.attempts else ""
        failure_key = "{}::{}".format(*scenario)
        if (
            len(releases) >= REPEATED_FAILURE_RELEASES
            and failure_key not in self._reported
        ):
            self._reported.add(failure_key)
            self._emit(
                EventKind.ANOMALY_REPEATED_SCENARIO_FAILURE,
                at,
                {
                    "feature": scenario[0],
                    "scenario": scenario[1],
                    "releases": sorted(releases),
                },
            )
        skips_key = EventKind.ANOMALY_REPEATED_SKIPS.value
        if (
            self._skipped >= REPEATED_SKIP_UNITS
            and skips_key not in self._reported
        ):
            self._reported.add(skips_key)
            self._emit(
                EventKind.ANOMALY_REPEATED_SKIPS,
                at,
                {"skipped": self._skipped},
            )

    def _emit(self, kind: str, at: str, data: dict[str, Any]) -> None:
        self._events.append(
            Event(
                seq=len(self._events) + 1,
                kind=kind,
                at=at,
                campaign_id=self._campaign_id,
                data=data,
            )
        )
