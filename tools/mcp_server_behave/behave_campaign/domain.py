"""Pure campaign rules: test units, attempts, and what to run next.

A unit is one behave scenario for one release on one machine type. This
module owns which units are in scope, what each attempt established, and the
order remaining work is taken up in. No file or process I/O lives here, so
the rules stay directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

PLAN = "plan"
ATTEMPT = "attempt"
CAMPAIGN = "campaign"
STATE = "state"
RETRY = "retry"

# Lifecycle. Only RUNNING, PAUSED and CANCELLED are ever written: CREATED is
# the absence of any state record, and COMPLETE is derived from the counts,
# so it cannot disagree with the units.
CREATED = "created"
RUNNING_STATE = "running"
PAUSED = "paused"
CANCELLED = "cancelled"
COMPLETE = "complete"
WRITABLE_LIFECYCLE = (RUNNING_STATE, PAUSED, CANCELLED)
LIFECYCLE_STATES = (CREATED, *WRITABLE_LIFECYCLE, COMPLETE)
UNATTEMPTED = "unattempted"
ATTEMPT_STATES = ("running", "passed", "failed", "skipped", "error")
PROBLEM_STATES = ("failed", "skipped", "error")
STATES = (UNATTEMPTED, *ATTEMPT_STATES)
INSTALL_SOURCES = (
    "local",
    "archive",
    "daily",
    "staging",
    "stable",
    "proposed",
)
DEFAULT_INSTALL_SOURCE = INSTALL_SOURCES[0]
UNIT_FIELDS = ("feature", "scenario", "release", "machine_type")
ATTEMPT_INPUT_FIELDS = (*UNIT_FIELDS, "state", "job_id")
ATTEMPT_FIELDS = (*ATTEMPT_INPUT_FIELDS, "install_from", "at")
PLAN_FIELDS = (*UNIT_FIELDS, "at")
STATE_FIELDS = ("state", "at", "reason")
RETRY_FIELDS = (*UNIT_FIELDS, "at", "reason")
CAMPAIGN_FIELDS = (
    "at",
    "campaign_id",
    "repo",
    "filters",
    "install_from",
    "max_lanes",
)
REPO_FIELDS = ("root", "commit", "branch", "dirty")
SCOPE_FIELDS = ("feature", "scenario", "release", "machine_type")
MCP_RUNNING_STATUSES = ("started", "timeout")
MCP_COMPLETED = "completed"


class CampaignError(ValueError):
    """Invalid campaign input or stored record."""


@dataclass(frozen=True, order=True)
class Unit:
    """One behave scenario for one release on one machine type."""

    feature: str
    scenario: str
    release: str
    machine_type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "feature": self.feature,
            "scenario": self.scenario,
            "release": self.release,
            "machine_type": self.machine_type,
        }


@dataclass(frozen=True)
class RepoState:
    """Which checkout a campaign was built from."""

    root: str = ""
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "commit": self.commit,
            "branch": self.branch,
            "dirty": self.dirty,
        }


@dataclass(frozen=True)
class PlanRecord:
    unit: Unit
    at: str = ""


@dataclass(frozen=True)
class AttemptRecord:
    unit: Unit
    state: str
    job_id: str
    install_from: str = ""
    at: str = ""


@dataclass(frozen=True)
class UnitStatus:
    unit: Unit
    state: str
    job_id: str | None
    attempts: tuple[AttemptRecord, ...]
    # Someone asked for another go at this unit since its last attempt. The
    # state still reports what actually happened; this is only about what
    # the scheduler may pick up.
    retry_pending: bool = False

    def as_dict(self, include_attempts: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = self.unit.as_dict()
        data["state"] = self.state
        data["job_id"] = self.job_id
        data["retry_pending"] = self.retry_pending
        if include_attempts:
            data["attempts"] = [
                {"state": attempt.state, "job_id": attempt.job_id}
                for attempt in self.attempts
            ]
        else:
            data["attempt_count"] = len(self.attempts)
        return data


@dataclass(frozen=True)
class Filters:
    feature: tuple[str, ...] = ()
    scenario: tuple[str, ...] = ()
    release: tuple[str, ...] = ()
    machine_type: tuple[str, ...] = ()
    state: tuple[str, ...] = ()

    def matches_unit(self, unit: Unit) -> bool:
        return (
            (not self.feature or unit.feature in self.feature)
            and (not self.scenario or unit.scenario in self.scenario)
            and (not self.release or unit.release in self.release)
            and (
                not self.machine_type or unit.machine_type in self.machine_type
            )
        )

    def matches(self, status: UnitStatus) -> bool:
        return self.matches_unit(status.unit) and (
            not self.state or status.state in self.state
        )

    def scope_as_dict(self) -> dict[str, list[str]]:
        return {
            "feature": list(self.feature),
            "scenario": list(self.scenario),
            "release": list(self.release),
            "machine_type": list(self.machine_type),
        }


@dataclass(frozen=True)
class CampaignHeader:
    """How a campaign was created, so it can be recreated and audited."""

    at: str = ""
    campaign_id: str | None = None
    repo: RepoState = RepoState()
    filters: Filters = Filters()
    # The install source every job runs with, and how many lanes a runner
    # may fill. Each attempt still records the source its own job actually
    # used, which is what a verification rests on.
    install_from: str = DEFAULT_INSTALL_SOURCE
    max_lanes: int = 1


@dataclass(frozen=True)
class RetryRecord:
    """A request for another attempt at a unit that already had one.

    Written rather than inferred, so the file says who wanted the rerun and
    when -- the scheduler never decides this for itself.
    """

    unit: Unit
    at: str = ""
    reason: str = ""


@dataclass(frozen=True)
class StateRecord:
    """A lifecycle transition someone asked for.

    ``reason`` explains transitions the server made on its own, such as
    pausing after a restart, and is empty for ones a caller asked for.
    """

    state: str
    at: str = ""
    reason: str = ""


Record = (
    CampaignHeader | PlanRecord | AttemptRecord | StateRecord | RetryRecord
)


def _require_fields(
    raw: Any, expected: Sequence[str], label: str
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CampaignError("each {} must be a JSON object".format(label))
    missing = sorted(set(expected) - set(raw))
    unknown = sorted(set(raw) - set(expected))
    if missing:
        raise CampaignError(
            "{} is missing fields: {}".format(label, ", ".join(missing))
        )
    if unknown:
        raise CampaignError(
            "{} has unknown fields: {}".format(label, ", ".join(unknown))
        )
    return raw


def _text(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(
            "{} field '{}' must be a non-empty string".format(label, key)
        )
    return value


def _unit_from(raw: dict[str, Any], label: str) -> Unit:
    return Unit(
        feature=_text(raw, "feature", label),
        scenario=_text(raw, "scenario", label),
        release=_text(raw, "release", label),
        machine_type=_text(raw, "machine_type", label),
    )


def parse_unit(raw: Any) -> Unit:
    return _unit_from(_require_fields(raw, UNIT_FIELDS, "unit"), "unit")


def parse_attempt(raw: Any) -> AttemptRecord:
    body = _require_fields(raw, ATTEMPT_INPUT_FIELDS, "attempt")
    state = body["state"]
    if state not in ATTEMPT_STATES:
        raise CampaignError(
            "attempt state must be one of: {}".format(
                ", ".join(ATTEMPT_STATES)
            )
        )
    return AttemptRecord(
        unit=_unit_from(body, "attempt"),
        state=state,
        job_id=_text(body, "job_id", "attempt"),
    )


def validate_campaign_id(value: str) -> str:
    """Return ``value`` if it is usable as a campaign id and a file name.

    A campaign id names a file on disk, so it is kept to characters that
    cannot escape the campaign directory or surprise a shell.
    """
    if not value or not value.strip():
        raise CampaignError("campaign id must not be empty")
    if not all(char.isalnum() or char in "._-" for char in value):
        raise CampaignError(
            "campaign id may only contain letters, digits, '.', '_' and "
            "'-'; got {!r}".format(value)
        )
    if value.startswith(".") or value in (".", ".."):
        raise CampaignError(
            "campaign id must not start with '.'; got {!r}".format(value)
        )
    return value


def validate_install_source(value: str) -> str:
    if value not in INSTALL_SOURCES:
        raise CampaignError(
            "install source must be one of: {}".format(
                ", ".join(INSTALL_SOURCES)
            )
        )
    return value


def _parse_stored_attempt(body: dict[str, Any]) -> AttemptRecord:
    _require_fields(body, ATTEMPT_FIELDS, "attempt")
    attempt = parse_attempt(
        {name: body[name] for name in ATTEMPT_INPUT_FIELDS}
    )
    return AttemptRecord(
        unit=attempt.unit,
        state=attempt.state,
        job_id=attempt.job_id,
        install_from=validate_install_source(
            _text(body, "install_from", "attempt")
        ),
        at=_text(body, "at", "attempt"),
    )


def _parse_campaign(body: dict[str, Any]) -> CampaignHeader:
    _require_fields(body, CAMPAIGN_FIELDS, "campaign")
    repo = _require_fields(body["repo"], REPO_FIELDS, "campaign repo")
    scope = _require_fields(body["filters"], SCOPE_FIELDS, "campaign filters")

    campaign_id = body["campaign_id"]
    if campaign_id is not None and not isinstance(campaign_id, str):
        raise CampaignError(
            "campaign field 'campaign_id' must be a string or null"
        )
    for name in ("commit", "branch"):
        if repo[name] is not None and not isinstance(repo[name], str):
            raise CampaignError(
                "campaign repo '{}' must be a string or null".format(name)
            )
    if repo["dirty"] is not None and not isinstance(repo["dirty"], bool):
        raise CampaignError("campaign repo 'dirty' must be a boolean or null")

    values: dict[str, tuple[str, ...]] = {}
    for name in SCOPE_FIELDS:
        if not isinstance(scope[name], list) or not all(
            isinstance(item, str) for item in scope[name]
        ):
            raise CampaignError(
                "campaign filter '{}' must be a list of strings".format(name)
            )
        values[name] = tuple(scope[name])

    install_from = validate_install_source(
        _text(body, "install_from", "campaign")
    )

    max_lanes = body["max_lanes"]
    if (
        isinstance(max_lanes, bool)
        or not isinstance(max_lanes, int)
        or max_lanes < 1
    ):
        raise CampaignError(
            "campaign field 'max_lanes' must be a positive integer"
        )

    return CampaignHeader(
        at=_text(body, "at", "campaign"),
        campaign_id=campaign_id,
        repo=RepoState(
            root=_text(repo, "root", "campaign repo"),
            commit=repo["commit"],
            branch=repo["branch"],
            dirty=repo["dirty"],
        ),
        filters=Filters(**values),
        install_from=install_from,
        max_lanes=max_lanes,
    )


def parse_record(raw: Any) -> Record:
    if not isinstance(raw, dict):
        raise CampaignError("each record must be a JSON object")
    kind = raw.get("type")
    body = {key: value for key, value in raw.items() if key != "type"}
    if kind == PLAN:
        _require_fields(body, PLAN_FIELDS, "plan")
        return PlanRecord(
            unit=parse_unit({f: body[f] for f in UNIT_FIELDS}),
            at=_text(body, "at", "plan"),
        )
    if kind == ATTEMPT:
        return _parse_stored_attempt(body)
    if kind == CAMPAIGN:
        return _parse_campaign(body)
    if kind == RETRY:
        _require_fields(body, RETRY_FIELDS, "retry")
        reason = body["reason"]
        if not isinstance(reason, str):
            raise CampaignError("retry field 'reason' must be a string")
        return RetryRecord(
            unit=parse_unit({f: body[f] for f in UNIT_FIELDS}),
            at=_text(body, "at", "retry"),
            reason=reason,
        )
    if kind == STATE:
        _require_fields(body, STATE_FIELDS, "state")
        state = body["state"]
        if state not in WRITABLE_LIFECYCLE:
            raise CampaignError(
                "state must be one of: {}".format(
                    ", ".join(WRITABLE_LIFECYCLE)
                )
            )
        reason = body["reason"]
        if not isinstance(reason, str):
            raise CampaignError("state field 'reason' must be a string")
        return StateRecord(
            state=state, at=_text(body, "at", "state"), reason=reason
        )
    raise CampaignError(
        "record type must be one of: {}, {}, {}, {}, {}".format(
            CAMPAIGN, PLAN, ATTEMPT, STATE, RETRY
        )
    )


def encode_record(record: Record) -> dict[str, Any]:
    if isinstance(record, CampaignHeader):
        return {
            "type": CAMPAIGN,
            "at": record.at,
            "campaign_id": record.campaign_id,
            "repo": record.repo.as_dict(),
            "filters": record.filters.scope_as_dict(),
            "install_from": record.install_from,
            "max_lanes": record.max_lanes,
        }

    if isinstance(record, StateRecord):
        return {
            "type": STATE,
            "state": record.state,
            "at": record.at,
            "reason": record.reason,
        }

    if isinstance(record, RetryRecord):
        retry: dict[str, Any] = record.unit.as_dict()
        retry["type"] = RETRY
        retry["at"] = record.at
        retry["reason"] = record.reason
        return retry

    data: dict[str, Any] = record.unit.as_dict()
    data["at"] = record.at
    if isinstance(record, AttemptRecord):
        data["type"] = ATTEMPT
        data["state"] = record.state
        data["job_id"] = record.job_id
        data["install_from"] = record.install_from
        return data
    data["type"] = PLAN
    return data


def _current_attempt(
    attempts: Sequence[AttemptRecord],
) -> AttemptRecord | None:
    for attempt in attempts:
        if attempt.state == "passed":
            return attempt
    return attempts[-1] if attempts else None


def reduce_units(records: Iterable[Record]) -> list[UnitStatus]:
    """Collapse records into one current status per planned unit."""
    planned: list[Unit] = []
    seen: set[Unit] = set()
    attempts: dict[Unit, list[AttemptRecord]] = {}
    # A retry asks for another go; the next attempt answers it. Reading the
    # records in order is what decides which of the two came last.
    pending: dict[Unit, bool] = {}
    for record in records:
        if isinstance(record, (CampaignHeader, StateRecord)):
            continue
        if isinstance(record, PlanRecord):
            if record.unit not in seen:
                seen.add(record.unit)
                planned.append(record.unit)
        elif isinstance(record, RetryRecord):
            pending[record.unit] = True
        else:
            attempts.setdefault(record.unit, []).append(record)
            pending[record.unit] = False

    statuses = []
    for unit in sorted(planned):
        unit_attempts = tuple(attempts.get(unit, ()))
        current = _current_attempt(unit_attempts)
        statuses.append(
            UnitStatus(
                unit=unit,
                state=current.state if current else UNATTEMPTED,
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
    unattempted = [s for s in statuses if s.state == UNATTEMPTED]
    retryable = [s for s in statuses if s.state in PROBLEM_STATES]
    return (unattempted + retryable)[:limit]


def is_schedulable(status: UnitStatus) -> bool:
    """Whether a scheduler may open a lane for this unit.

    Either it has never been attempted, or someone has asked for another go
    since its last attempt. A failed unit is not schedulable on its own:
    the scheduler runs unattended, and judging a failure flaky-or-real is
    for whoever is watching, not for the tick loop.
    """
    if status.state == "running":
        return False
    return status.state == UNATTEMPTED or status.retry_pending


def select_schedulable(
    statuses: Sequence[UnitStatus], limit: int
) -> list[UnitStatus]:
    """The units a scheduler may open lanes for, untouched ones first."""
    if limit < 1:
        raise CampaignError("limit must be positive")
    ready = [s for s in statuses if is_schedulable(s)]
    ready.sort(key=lambda s: s.state != UNATTEMPTED)
    return ready[:limit]


def count_states(statuses: Iterable[UnitStatus]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    for status in statuses:
        counts[status.state] += 1
    return counts


def problems(statuses: Iterable[UnitStatus]) -> list[UnitStatus]:
    return [s for s in statuses if s.state in PROBLEM_STATES]


def running(statuses: Iterable[UnitStatus]) -> list[UnitStatus]:
    return [s for s in statuses if s.state == "running"]


def _completed_state(result: dict[str, Any]) -> str:
    summary = result.get("summary")
    if summary is None:
        return "error"
    if not isinstance(summary, dict):
        raise CampaignError("MCP result 'summary' must be an object or null")

    raw_counts = summary.get("scenarios")
    if not isinstance(raw_counts, dict) or not raw_counts:
        raise CampaignError("MCP result reports no scenario counts")

    counts: dict[str, int] = {}
    for name, value in raw_counts.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise CampaignError(
                "scenario count '{}' must be an integer".format(name)
            )
        counts[name] = value

    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    skipped = counts.get("skipped", 0)
    other = {
        name: count
        for name, count in counts.items()
        if count and name not in ("passed", "failed", "skipped", "total")
    }

    if not other:
        if failed:
            return "failed"
        if passed:
            if result.get("ok") is False:
                raise CampaignError(
                    "MCP reports ok=false but every scenario passed"
                )
            return "passed"
        if skipped and not passed:
            return "skipped"

    raise CampaignError(
        "cannot classify scenario counts {}; record an explicit state".format(
            counts
        )
    )


def attempt_from_mcp(unit: Unit, result: Any) -> AttemptRecord:
    """Map one MCP start or wait payload onto a single attempt."""
    if not isinstance(result, dict):
        raise CampaignError("each MCP result must be a JSON object")

    status = result.get("status")
    if status not in (*MCP_RUNNING_STATUSES, MCP_COMPLETED):
        raise CampaignError(
            "cannot record an attempt for MCP status '{}'".format(status)
        )
    if "job_id" not in result:
        raise CampaignError("MCP result is missing 'job_id'")

    state = _completed_state(result) if status == MCP_COMPLETED else "running"
    return AttemptRecord(
        unit=unit,
        state=state,
        job_id=_text(result, "job_id", "MCP result"),
    )


def attempts_from_mcp(payload: Any) -> list[AttemptRecord]:
    """Map ``[{"unit": ..., "result": ...}]`` MCP payloads onto attempts."""
    if not isinstance(payload, list) or not payload:
        raise CampaignError("input must be a non-empty JSON array")

    attempts = []
    for entry in payload:
        body = _require_fields(entry, ("unit", "result"), "MCP entry")
        attempts.append(
            attempt_from_mcp(parse_unit(body["unit"]), body["result"])
        )
    return attempts


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
    """Return a campaign's current lifecycle state.

    ``complete`` is derived rather than written: a running campaign with
    nothing left unattempted and nothing in flight is finished, whether or
    not anyone noticed. Problem units do not count as remaining work,
    because a non-passing unit is only ever retried when asked for.
    """
    current = CREATED
    for record in records:
        if isinstance(record, StateRecord):
            current = record.state

    if current != RUNNING_STATE:
        return current

    statuses = reduce_units(records)
    outstanding = [
        status
        for status in statuses
        if status.state in (UNATTEMPTED, "running") or status.retry_pending
    ]
    return RUNNING_STATE if outstanding else COMPLETE


def last_state_reason(records: Sequence[Record]) -> str:
    """Return the reason on the most recent lifecycle transition."""
    reason = ""
    for record in records:
        if isinstance(record, StateRecord):
            reason = record.reason
    return reason


@dataclass(frozen=True)
class Classification:
    """One finished job mapped onto an attempt.

    ``problem`` is set when the job's result could not be classified. The
    attempt is still recorded, as ``error``, because a scheduler has nobody
    to raise at and one strange job must not stop a campaign.
    """

    attempt: AttemptRecord
    problem: str = ""


def classify_result(
    unit: Unit,
    result: Any,
    at: str = "",
    install_from: str = DEFAULT_INSTALL_SOURCE,
) -> Classification:
    """Map an MCP job payload onto an attempt. Never raises."""
    try:
        attempt = attempt_from_mcp(unit, result)
    except CampaignError as error:
        job_id = ""
        if isinstance(result, dict):
            raw_job_id = result.get("job_id")
            job_id = raw_job_id if isinstance(raw_job_id, str) else ""
        return Classification(
            attempt=AttemptRecord(
                unit=unit,
                state="error",
                job_id=job_id,
                install_from=install_from,
                at=at,
            ),
            problem=str(error),
        )
    return Classification(
        attempt=AttemptRecord(
            unit=attempt.unit,
            state=attempt.state,
            job_id=attempt.job_id,
            install_from=install_from,
            at=at,
        )
    )


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
    which is the common case.
    """

    record: tuple[Classification, ...] = ()
    start: tuple[Unit, ...] = ()
    lifecycle: str = CREATED

    @property
    def idle(self) -> bool:
        return not self.record and not self.start


def plan_tick(
    *,
    records: Sequence[Record],
    lanes: Sequence[Lane],
    max_lanes: int,
    at: str = "",
) -> TickPlan:
    """Decide what to do next. Pure: starts nothing and writes nothing.

    Finished lanes are classified first, so the units they free are
    available to fill in the same tick. New lanes are opened only while the
    campaign is running, which is what makes a paused campaign drain: its
    in-flight jobs are still recorded, and nothing new begins.
    """
    classified = tuple(
        classify_result(lane.unit, lane.result, at, lane.install_from)
        for lane in lanes
        if lane.finished
    )

    after = [*records, *(item.attempt for item in classified)]
    state = lifecycle(after)
    if state != RUNNING_STATE:
        return TickPlan(record=classified, lifecycle=state)

    busy = sum(1 for lane in lanes if not lane.finished)
    free = max(max_lanes - busy, 0)
    if not free:
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


# Event kinds, grouped into families so a subscriber can ask for "unit.*"
# rather than enumerating outcomes. New kinds are additive: a client that
# does not recognise one ignores it.
CAMPAIGN_CREATED = "campaign.created"
CAMPAIGN_STARTED = "campaign.started"
CAMPAIGN_PAUSED = "campaign.paused"
CAMPAIGN_RESUMED = "campaign.resumed"
CAMPAIGN_CANCELLED = "campaign.cancelled"
CAMPAIGN_COMPLETE = "campaign.complete"
LANE_STARTED = "lane.started"
LANE_RELEASED = "lane.released"
UNIT_PASSED = "unit.passed"
UNIT_FAILED = "unit.failed"
UNIT_SKIPPED = "unit.skipped"
UNIT_ERRORED = "unit.errored"
UNIT_UNCLASSIFIABLE = "unit.unclassifiable"
UNIT_RETRIED = "unit.retried"
LANE_OVERDUE = "lane.overdue"
ANOMALY_REPEATED_SCENARIO_FAILURE = "anomaly.repeated_scenario_failure"
ANOMALY_REPEATED_SKIPS = "anomaly.repeated_skips"
ANOMALY_CAPACITY_STARVED = "anomaly.capacity_starved"

EVENT_KINDS = (
    CAMPAIGN_CREATED,
    CAMPAIGN_STARTED,
    CAMPAIGN_PAUSED,
    CAMPAIGN_RESUMED,
    CAMPAIGN_CANCELLED,
    CAMPAIGN_COMPLETE,
    LANE_STARTED,
    LANE_RELEASED,
    UNIT_PASSED,
    UNIT_FAILED,
    UNIT_SKIPPED,
    UNIT_ERRORED,
    UNIT_UNCLASSIFIABLE,
    UNIT_RETRIED,
    LANE_OVERDUE,
    ANOMALY_REPEATED_SCENARIO_FAILURE,
    ANOMALY_REPEATED_SKIPS,
    ANOMALY_CAPACITY_STARVED,
)
EVENT_FAMILIES = ("campaign", "lane", "unit", "anomaly")

# Which unit event an attempt state produces.
_UNIT_EVENT_FOR_STATE = {
    "passed": UNIT_PASSED,
    "failed": UNIT_FAILED,
    "skipped": UNIT_SKIPPED,
    "error": UNIT_ERRORED,
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
    body = _require_fields(
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
        kind=_text(body, "kind", "event"),
        at=_text(body, "at", "event"),
        campaign_id=_text(body, "campaign_id", "event"),
        data=data,
    )


def validate_event_kinds(kinds: Sequence[str]) -> tuple[str, ...]:
    """Return the subscription patterns, rejecting ones that match nothing.

    A typo in a filter would otherwise look like a campaign that never emits
    anything, which is the most confusing failure available here.
    """
    for pattern in kinds:
        if pattern.endswith(".*"):
            if pattern[:-2] in EVENT_FAMILIES:
                continue
            raise CampaignError(
                "unknown event family {!r}; known families: {}".format(
                    pattern[:-2], ", ".join(EVENT_FAMILIES)
                )
            )
        elif pattern not in EVENT_KINDS:
            raise CampaignError(
                "unknown event kind {!r}; known kinds: {}".format(
                    pattern, ", ".join(EVENT_KINDS)
                )
            )
    return tuple(kinds)


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


def unit_event_kind(state: str, unclassifiable: bool = False) -> str:
    """The event kind an attempt outcome produces."""
    if unclassifiable:
        return UNIT_UNCLASSIFIABLE
    return _UNIT_EVENT_FOR_STATE.get(state, UNIT_ERRORED)


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
        if status.state == "failed":
            failing = (status.unit.feature, status.unit.scenario)
            failed_releases.setdefault(failing, set()).add(status.unit.release)
        elif status.state == "skipped":
            skipped += 1

    for (feature, scenario), releases in sorted(failed_releases.items()):
        if len(releases) < REPEATED_FAILURE_RELEASES:
            continue
        signals.append(
            Signal(
                kind=ANOMALY_REPEATED_SCENARIO_FAILURE,
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
                kind=ANOMALY_REPEATED_SKIPS,
                key=ANOMALY_REPEATED_SKIPS,
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
                kind=LANE_OVERDUE,
                key=lane.job_id,
                data={
                    **lane.unit.as_dict(),
                    "job_id": lane.job_id,
                    "elapsed_seconds": int(age),
                },
            )
        )

    return signals
