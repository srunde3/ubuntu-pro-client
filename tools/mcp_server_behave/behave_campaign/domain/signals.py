"""Derived observations offered to whoever watches a campaign."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from behave_campaign.domain.events import EventKind
from behave_campaign.domain.records import UnitState, UnitStatus
from behave_campaign.domain.schedule import Lane

# Thresholds for the derived signals. Each is a judgement call offered to
# whoever is watching, never something the scheduler acts on itself.
DEFAULT_OVERDUE_SECONDS = 3600.0


REPEATED_FAILURE_RELEASES = 3


REPEATED_SKIP_UNITS = 5


_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Signal:
    """A derived observation worth telling someone about.

    ``key`` identifies the condition rather than the moment, so a signal
    that stays true is reported once instead of on every tick.
    """

    kind: str
    key: str
    data: dict[str, Any] = field(default_factory=dict)


def elapsed_seconds(since: str, now: str) -> float | None:
    """Seconds between two campaign timestamps, or None if unreadable."""
    try:
        start = datetime.strptime(since, _TIMESTAMP_FORMAT)
        end = datetime.strptime(now, _TIMESTAMP_FORMAT)
    except (ValueError, TypeError):
        return None
    return (end - start).total_seconds()


def detect_signals(
    *,
    statuses: Sequence[UnitStatus],
    lanes: Sequence[Lane],
    at: str,
    overdue_seconds: float = DEFAULT_OVERDUE_SECONDS,
) -> list[Signal]:
    """Derive the signals a watcher should judge. Pure; acts on nothing.

    None of these stop a campaign. A run of skips usually means a config
    the host does not have, and one scenario failing across every release
    usually means a real defect rather than flake -- but which of those it
    is, and what to do, is not for an unattended loop to decide.
    """
    signals: list[Signal] = []

    failed_releases: dict[tuple[str, str], set[str]] = {}
    skipped = 0
    for status in statuses:
        if status.state == UnitState.FAILED:
            failing = (status.unit.feature, status.unit.scenario)
            failed_releases.setdefault(failing, set()).add(status.unit.release)
        elif status.state == UnitState.SKIPPED:
            skipped += 1

    for (feature, scenario), releases in sorted(failed_releases.items()):
        if len(releases) < REPEATED_FAILURE_RELEASES:
            continue
        signals.append(
            Signal(
                kind=EventKind.ANOMALY_REPEATED_SCENARIO_FAILURE,
                key="{}::{}".format(feature, scenario),
                data={
                    "feature": feature,
                    "scenario": scenario,
                    "releases": sorted(releases),
                },
            )
        )

    if skipped >= REPEATED_SKIP_UNITS:
        signals.append(
            Signal(
                kind=EventKind.ANOMALY_REPEATED_SKIPS,
                key=EventKind.ANOMALY_REPEATED_SKIPS,
                data={"skipped": skipped},
            )
        )

    for lane in lanes:
        if lane.finished:
            continue
        age = elapsed_seconds(lane.opened_at, at)
        if age is None or age < overdue_seconds:
            continue
        signals.append(
            Signal(
                kind=EventKind.LANE_OVERDUE,
                key=lane.job_id,
                data={
                    **lane.unit.as_dict(),
                    "job_id": lane.job_id,
                    "elapsed_seconds": int(age),
                },
            )
        )

    return signals
