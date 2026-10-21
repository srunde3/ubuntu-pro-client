"""Thresholds and checks for the signals offered to a campaign's watcher.

None of these stop a campaign. A run of skips usually means a config the
host does not have, and one scenario failing across every release usually
means a real defect rather than flake -- but which of those it is, and what
to do, is not for an unattended loop to decide.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

from behave_campaign.domain.records import OverdueRecord
from behave_campaign.domain.schedule import Lane

DEFAULT_OVERDUE_SECONDS = 3600.0


# A scenario failing on this many releases at once is announced.
REPEATED_FAILURE_RELEASES = 3


# This many units skipped at once is announced.
REPEATED_SKIP_UNITS = 5


def _parse(timestamp: str) -> datetime:
    """An ISO 8601 UTC time, with ``Z`` and any precision."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def elapsed_seconds(since: str, now: str) -> float | None:
    """Seconds between two timestamps, or None if unreadable."""
    try:
        start = _parse(since)
        end = _parse(now)
    except (ValueError, TypeError, AttributeError):
        return None
    return (end - start).total_seconds()


def overdue_lanes(
    lanes: Sequence[Lane],
    *,
    at: str,
    overdue_seconds: float,
    reported: Iterable[str],
) -> list[OverdueRecord]:
    """Records for lanes running past ``overdue_seconds`` and not yet
    reported."""
    already = set(reported)
    found = []
    for lane in lanes:
        if lane.finished or lane.job_id in already:
            continue
        age = elapsed_seconds(lane.opened_at, at)
        if age is None or age < overdue_seconds:
            continue
        found.append(
            OverdueRecord(
                unit=lane.unit,
                job_id=lane.job_id,
                elapsed_seconds=int(age),
                at=at,
            )
        )
    return found
