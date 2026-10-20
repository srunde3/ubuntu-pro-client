"""What a campaign announces, and how a subscriber selects it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from behave_campaign.domain.records import (
    CampaignError,
    Outcome,
    StringEnum,
    require_fields,
    text_field,
)


class EventFamily(StringEnum):
    """The groups a subscriber can ask for wholesale, as ``unit.*``."""

    CAMPAIGN = "campaign"
    LANE = "lane"
    UNIT = "unit"
    ANOMALY = "anomaly"


class EventKind(StringEnum):
    """What a campaign announces.

    New kinds are additive: a subscriber that does not recognise one ignores
    it, and one asking for a family gets it without asking again.
    """

    CAMPAIGN_CREATED = "campaign.created"
    CAMPAIGN_STARTED = "campaign.started"
    CAMPAIGN_PAUSED = "campaign.paused"
    CAMPAIGN_RESUMED = "campaign.resumed"
    CAMPAIGN_CANCELLED = "campaign.cancelled"
    CAMPAIGN_REOPENED = "campaign.reopened"
    CAMPAIGN_COMPLETE = "campaign.complete"
    LANE_STARTED = "lane.started"
    LANE_OVERDUE = "lane.overdue"
    UNIT_PASSED = "unit.passed"
    UNIT_FAILED = "unit.failed"
    UNIT_SKIPPED = "unit.skipped"
    UNIT_ERRORED = "unit.errored"
    UNIT_UNCLASSIFIABLE = "unit.unclassifiable"
    UNIT_RETRIED = "unit.retried"
    ANOMALY_REPEATED_SCENARIO_FAILURE = "anomaly.repeated_scenario_failure"
    ANOMALY_REPEATED_SKIPS = "anomaly.repeated_skips"
    ANOMALY_CAPACITY_STARVED = "anomaly.capacity_starved"

    @property
    def family(self) -> EventFamily:
        return EventFamily(self.value.split(".", 1)[0])


EVENT_KINDS = tuple(kind.value for kind in EventKind)


EVENT_FAMILIES = tuple(family.value for family in EventFamily)


# Subscription presets. ``actionable`` is what a watcher has to react to:
# every non-passing outcome, every derived signal, and the campaign's own
# transitions. Passes, lane openings and the watcher's own retries are left
# out; the counts on every response carry progress.
EVERYTHING_PRESET = "*"


ACTIONABLE_PRESET = "actionable"


ACTIONABLE_KINDS = (
    EventKind.UNIT_FAILED.value,
    EventKind.UNIT_ERRORED.value,
    EventKind.UNIT_SKIPPED.value,
    EventKind.UNIT_UNCLASSIFIABLE.value,
    EventFamily.ANOMALY.value + ".*",
    EventKind.LANE_OVERDUE.value,
    EventFamily.CAMPAIGN.value + ".*",
)


EVENT_PRESETS = (EVERYTHING_PRESET, ACTIONABLE_PRESET)


# Which unit event an outcome produces.
_UNIT_EVENT_FOR_OUTCOME = {
    Outcome.PASSED: EventKind.UNIT_PASSED,
    Outcome.FAILED: EventKind.UNIT_FAILED,
    Outcome.SKIPPED: EventKind.UNIT_SKIPPED,
    Outcome.ERROR: EventKind.UNIT_ERRORED,
}


# Enough of a failure to triage without fetching the job's report.
MAX_EVENT_FAILURES = 3


MAX_EVENT_ERROR_CHARS = 1200


@dataclass(frozen=True)
class NewEvent:
    """An event on its way to the log, before the log numbers it."""

    kind: str
    at: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    """A numbered event. ``seq`` is monotonic within one campaign."""

    seq: int
    kind: str
    at: str
    campaign_id: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "campaign_id": self.campaign_id,
            "data": dict(self.data),
        }


def parse_event(raw: Any) -> Event:
    """Read one stored event back."""
    body = require_fields(
        raw, ("seq", "kind", "at", "campaign_id", "data"), "event"
    )
    seq = body["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise CampaignError("event field 'seq' must be a positive integer")
    data = body["data"]
    if not isinstance(data, dict):
        raise CampaignError("event field 'data' must be an object")
    return Event(
        seq=seq,
        kind=text_field(body, "kind", "event"),
        at=text_field(body, "at", "event"),
        campaign_id=text_field(body, "campaign_id", "event"),
        data=data,
    )


def expand_event_kinds(kinds: Sequence[str]) -> tuple[str, ...]:
    """Return the subscription patterns, rejecting ones that match nothing.

    Each entry is an exact kind, a family (``unit.*``), or a preset:
    ``actionable`` expands to :data:`ACTIONABLE_KINDS` and ``*`` to no
    pattern at all, which is everything. A typo would otherwise look like a
    campaign that never emits anything, which is the most confusing failure
    available here.
    """
    patterns: list[str] = []
    for pattern in kinds:
        if pattern == EVERYTHING_PRESET:
            return ()
        if pattern == ACTIONABLE_PRESET:
            patterns.extend(ACTIONABLE_KINDS)
        elif pattern.endswith(".*"):
            if pattern[:-2] not in EVENT_FAMILIES:
                raise CampaignError(
                    "unknown event family {!r}; known families: {}".format(
                        pattern[:-2], ", ".join(EVENT_FAMILIES)
                    )
                )
            patterns.append(pattern)
        elif pattern in EVENT_KINDS:
            patterns.append(pattern)
        else:
            raise CampaignError(
                "unknown event kind {!r}; known kinds: {}; presets: {}".format(
                    pattern, ", ".join(EVENT_KINDS), ", ".join(EVENT_PRESETS)
                )
            )
    return tuple(patterns)


def event_matches(kind: str, patterns: Sequence[str]) -> bool:
    """Whether ``kind`` is wanted. No patterns means everything is."""
    if not patterns:
        return True
    for pattern in patterns:
        if pattern.endswith(".*"):
            if kind.startswith(pattern[:-1]):
                return True
        elif kind == pattern:
            return True
    return False


def unit_event_kind(outcome: str, unclassifiable: bool = False) -> str:
    """The event kind an outcome produces."""
    if unclassifiable:
        return EventKind.UNIT_UNCLASSIFIABLE
    return _UNIT_EVENT_FOR_OUTCOME.get(
        Outcome(outcome), EventKind.UNIT_ERRORED
    )


def failure_details(result: Any) -> list[dict[str, str]]:
    """Pull the failing steps out of a job result, for a unit.failed event.

    Carried on the event so a caller can judge a failure without going back
    for the job's report.
    """
    if not isinstance(result, dict):
        return []
    raw = result.get("failures")
    if not isinstance(raw, list):
        return []
    details = []
    for failure in raw[:MAX_EVENT_FAILURES]:
        if not isinstance(failure, dict):
            continue
        details.append(
            {
                "step": str(failure.get("step", "")),
                "status": str(failure.get("status", "")),
                "error_message": str(failure.get("error_message", ""))[
                    :MAX_EVENT_ERROR_CHARS
                ],
            }
        )
    return details


def shorten_failures(data: dict[str, Any], chars: int) -> dict[str, Any]:
    """A copy of an event body with each failure's message cut to ``chars``."""
    failures = data.get("failures")
    if not isinstance(failures, list) or chars >= MAX_EVENT_ERROR_CHARS:
        return dict(data)
    return {
        **data,
        "failures": [
            (
                {**item, "error_message": str(item["error_message"])[:chars]}
                if isinstance(item, dict) and "error_message" in item
                else item
            )
            for item in failures
        ],
    }
