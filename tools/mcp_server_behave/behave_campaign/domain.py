"""Pure campaign rules: test units, attempts, and what to run next.

A unit is one behave scenario for one release on one machine type. This
module owns which units are in scope, what each attempt established, and the
order remaining work is taken up in. No file or process I/O lives here, so
the rules stay directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Sequence


class _StringEnum(str, Enum):
    """A string enum that reads as its value.

    ``Enum`` renders a member as ``Class.MEMBER``, which would put
    "Lifecycle.PAUSED" into error messages a person reads and JSON a caller
    parses. ``enum.StrEnum`` would do this for us but needs Python 3.11, and
    this package supports 3.10.
    """

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


class RecordType(_StringEnum):
    """The kinds of record an append-only campaign file holds."""

    CAMPAIGN = "campaign"
    PLAN = "plan"
    # One try at a unit is one job, written as two records: a job starts,
    # and later it finishes. Both are needed -- the opening record is what
    # holds the lane, including across a restart -- but neither is an
    # attempt on its own. Attempt pairs them on job_id.
    STARTED = "started"
    FINISHED = "finished"
    LIFECYCLE = "lifecycle"
    RETRY = "retry"


class Outcome(_StringEnum):
    """What a finished job established.

    "running" is not among them: a unit is running when its latest attempt
    has no finish yet, which is a fact about the attempt rather than a
    result the job reported.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class UnitState(_StringEnum):
    """A unit's condition, derived from its attempts rather than stored."""

    UNATTEMPTED = "unattempted"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class Lifecycle(_StringEnum):
    """Where a campaign is in its life.

    CREATED is the absence of any lifecycle record and COMPLETE is derived
    from the counts, so neither is ever written; the other three are.
    """

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETE = "complete"


# The states a caller can ask for, as opposed to the two that are derived.
WRITABLE_LIFECYCLE = (
    Lifecycle.RUNNING,
    Lifecycle.PAUSED,
    Lifecycle.CANCELLED,
)
# Outcomes worth another look. A unit in one of these is only ever re-run
# when someone asks.
PROBLEM_OUTCOMES = (Outcome.FAILED, Outcome.SKIPPED, Outcome.ERROR)
# Tuples of plain strings, for argparse choices and error messages.
RECORD_TYPES = tuple(kind.value for kind in RecordType)
OUTCOMES = tuple(outcome.value for outcome in Outcome)
STATES = tuple(state.value for state in UnitState)
LIFECYCLE_STATES = tuple(state.value for state in Lifecycle)
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
STARTED_FIELDS = (*UNIT_FIELDS, "job_id", "install_from", "at")
FINISHED_FIELDS = (*UNIT_FIELDS, "job_id", "outcome", "at")
# What a caller hands to record_attempts: one completed try.
FINISHED_INPUT_FIELDS = (*UNIT_FIELDS, "job_id", "outcome")
PLAN_FIELDS = (*UNIT_FIELDS, "at")
LIFECYCLE_FIELDS = ("state", "at", "reason")
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
class AttemptStarted:
    """A job began for this unit. Written as the lane opens."""

    unit: Unit
    job_id: str
    install_from: str = ""
    at: str = ""


@dataclass(frozen=True)
class AttemptFinished:
    """That job ended, and what it established."""

    unit: Unit
    job_id: str
    outcome: Outcome
    at: str = ""


@dataclass(frozen=True)
class Attempt:
    """One try at a unit: one job, from start to finish.

    Derived rather than stored, by pairing a ``started`` record with the
    ``finished`` one that shares its ``job_id``. This is what "attempt"
    means to a caller, so counting these counts tries.
    """

    job_id: str
    install_from: str = ""
    started_at: str = ""
    outcome: str | None = None
    finished_at: str = ""

    @property
    def running(self) -> bool:
        return self.outcome is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "install_from": self.install_from,
            "started_at": self.started_at,
            "outcome": self.outcome,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class UnitStatus:
    unit: Unit
    state: str
    job_id: str | None
    attempts: tuple[Attempt, ...]
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
            data["attempts"] = [attempt.as_dict() for attempt in self.attempts]
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
class LifecycleRecord:
    """A lifecycle transition someone asked for.

    ``reason`` explains transitions the server made on its own, such as
    pausing after a restart, and is empty for ones a caller asked for.
    """

    state: Lifecycle
    at: str = ""
    reason: str = ""


Record = (
    CampaignHeader
    | PlanRecord
    | AttemptStarted
    | AttemptFinished
    | LifecycleRecord
    | RetryRecord
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


def validate_outcome(value: Any) -> Outcome:
    try:
        return Outcome(value)
    except ValueError:
        raise CampaignError(
            "outcome must be one of: {}".format(", ".join(OUTCOMES))
        ) from None


def parse_finished(raw: Any) -> AttemptFinished:
    """Read one completed try as a caller describes it."""
    body = _require_fields(raw, FINISHED_INPUT_FIELDS, "attempt")
    return AttemptFinished(
        unit=_unit_from(body, "attempt"),
        job_id=_text(body, "job_id", "attempt"),
        outcome=validate_outcome(body["outcome"]),
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


def _parse_started(body: dict[str, Any]) -> AttemptStarted:
    _require_fields(body, STARTED_FIELDS, "started")
    return AttemptStarted(
        unit=_unit_from(body, "started"),
        job_id=_text(body, "job_id", "started"),
        install_from=validate_install_source(
            _text(body, "install_from", "started")
        ),
        at=_text(body, "at", "started"),
    )


def _parse_finished(body: dict[str, Any]) -> AttemptFinished:
    _require_fields(body, FINISHED_FIELDS, "finished")
    return AttemptFinished(
        unit=_unit_from(body, "finished"),
        job_id=_text(body, "job_id", "finished"),
        outcome=validate_outcome(body["outcome"]),
        at=_text(body, "at", "finished"),
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
    if kind == RecordType.PLAN:
        _require_fields(body, PLAN_FIELDS, "plan")
        return PlanRecord(
            unit=parse_unit({f: body[f] for f in UNIT_FIELDS}),
            at=_text(body, "at", "plan"),
        )
    if kind == RecordType.STARTED:
        return _parse_started(body)
    if kind == RecordType.FINISHED:
        return _parse_finished(body)
    if kind == RecordType.CAMPAIGN:
        return _parse_campaign(body)
    if kind == RecordType.RETRY:
        _require_fields(body, RETRY_FIELDS, "retry")
        reason = body["reason"]
        if not isinstance(reason, str):
            raise CampaignError("retry field 'reason' must be a string")
        return RetryRecord(
            unit=parse_unit({f: body[f] for f in UNIT_FIELDS}),
            at=_text(body, "at", "retry"),
            reason=reason,
        )
    if kind == RecordType.LIFECYCLE:
        _require_fields(body, LIFECYCLE_FIELDS, "lifecycle")
        state = body["state"]
        if state not in WRITABLE_LIFECYCLE:
            raise CampaignError(
                "lifecycle state must be one of: {}".format(
                    ", ".join(WRITABLE_LIFECYCLE)
                )
            )
        reason = body["reason"]
        if not isinstance(reason, str):
            raise CampaignError("lifecycle field 'reason' must be a string")
        return LifecycleRecord(
            state=Lifecycle(state),
            at=_text(body, "at", "lifecycle"),
            reason=reason,
        )
    raise CampaignError(
        "record type must be one of: {}".format(", ".join(RECORD_TYPES))
    )


def encode_record(record: Record) -> dict[str, Any]:
    if isinstance(record, CampaignHeader):
        return {
            "type": RecordType.CAMPAIGN,
            "at": record.at,
            "campaign_id": record.campaign_id,
            "repo": record.repo.as_dict(),
            "filters": record.filters.scope_as_dict(),
            "install_from": record.install_from,
            "max_lanes": record.max_lanes,
        }

    if isinstance(record, LifecycleRecord):
        return {
            "type": RecordType.LIFECYCLE,
            "state": record.state,
            "at": record.at,
            "reason": record.reason,
        }

    if isinstance(record, RetryRecord):
        retry: dict[str, Any] = record.unit.as_dict()
        retry["type"] = RecordType.RETRY
        retry["at"] = record.at
        retry["reason"] = record.reason
        return retry

    data: dict[str, Any] = record.unit.as_dict()
    data["at"] = record.at
    if isinstance(record, AttemptStarted):
        data["type"] = RecordType.STARTED
        data["job_id"] = record.job_id
        data["install_from"] = record.install_from
        return data
    if isinstance(record, AttemptFinished):
        data["type"] = RecordType.FINISHED
        data["job_id"] = record.job_id
        data["outcome"] = record.outcome
        return data
    data["type"] = RecordType.PLAN
    return data


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
    statuses: Sequence[UnitStatus], limit: int
) -> list[UnitStatus]:
    """The units a scheduler may open lanes for, untouched ones first."""
    if limit < 1:
        raise CampaignError("limit must be positive")
    ready = [s for s in statuses if is_schedulable(s)]
    ready.sort(key=lambda s: s.state != UnitState.UNATTEMPTED)
    return ready[:limit]


def count_states(statuses: Iterable[UnitStatus]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    for status in statuses:
        counts[status.state] += 1
    return counts


def problems(statuses: Iterable[UnitStatus]) -> list[UnitStatus]:
    return [s for s in statuses if s.state in PROBLEM_OUTCOMES]


class GroupBy(_StringEnum):
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


def _completed_outcome(result: dict[str, Any]) -> Outcome:
    summary = result.get("summary")
    if summary is None:
        return Outcome.ERROR
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
            return Outcome.FAILED
        if passed:
            if result.get("ok") is False:
                raise CampaignError(
                    "MCP reports ok=false but every scenario passed"
                )
            return Outcome.PASSED
        if skipped and not passed:
            return Outcome.SKIPPED

    raise CampaignError(
        "cannot classify scenario counts {}; record an explicit "
        "outcome".format(counts)
    )


def finished_from_mcp(unit: Unit, result: Any) -> AttemptFinished:
    """Map one completed MCP job payload onto a finished attempt.

    Only a completed job can be recorded this way. A job still in flight is
    the scheduler's business -- it wrote the ``started`` record itself and
    will write the finish -- so handing one here means something is being
    recorded that nobody has an outcome for yet.
    """
    if not isinstance(result, dict):
        raise CampaignError("each MCP result must be a JSON object")

    status = result.get("status")
    if status != MCP_COMPLETED:
        raise CampaignError(
            "cannot record an outcome for MCP status '{}'; only a "
            "completed job has one".format(status)
        )
    if "job_id" not in result:
        raise CampaignError("MCP result is missing 'job_id'")

    return AttemptFinished(
        unit=unit,
        job_id=_text(result, "job_id", "MCP result"),
        outcome=_completed_outcome(result),
    )


def finished_from_mcp_payload(payload: Any) -> list[AttemptFinished]:
    """Map ``[{"unit": ..., "result": ...}]`` MCP payloads onto outcomes."""
    if not isinstance(payload, list) or not payload:
        raise CampaignError("input must be a non-empty JSON array")

    finished = []
    for entry in payload:
        body = _require_fields(entry, ("unit", "result"), "MCP entry")
        finished.append(
            finished_from_mcp(parse_unit(body["unit"]), body["result"])
        )
    return finished


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

    The latest lifecycle record wins, which is what lets a cancellation be
    reversed: the ``cancelled`` record stays where it is and a ``running``
    one is appended after it, so the campaign goes back to work with both
    still readable.
    """
    current = Lifecycle.CREATED
    for record in records:
        if isinstance(record, LifecycleRecord):
            current = record.state

    if current != Lifecycle.RUNNING:
        return current

    statuses = reduce_units(records)
    outstanding = [
        status
        for status in statuses
        if status.state in (UnitState.UNATTEMPTED, UnitState.RUNNING)
        or status.retry_pending
    ]
    return Lifecycle.RUNNING if outstanding else Lifecycle.COMPLETE


def last_state_reason(records: Sequence[Record]) -> str:
    """Return the reason on the most recent lifecycle transition."""
    reason = ""
    for record in records:
        if isinstance(record, LifecycleRecord):
            reason = record.reason
    return reason


@dataclass(frozen=True)
class Classification:
    """One finished job mapped onto an attempt.

    ``problem`` is set when the job's result could not be classified. The
    outcome is still recorded, as ``error``, because a scheduler has nobody
    to raise at and one strange job must not stop a campaign.
    """

    finished: AttemptFinished
    problem: str = ""


def classify_result(unit: Unit, result: Any, at: str = "") -> Classification:
    """Map a finished MCP job onto an outcome. Never raises."""
    try:
        finished = finished_from_mcp(unit, result)
    except CampaignError as error:
        job_id = ""
        if isinstance(result, dict):
            raw_job_id = result.get("job_id")
            job_id = raw_job_id if isinstance(raw_job_id, str) else ""
        return Classification(
            finished=AttemptFinished(
                unit=unit,
                job_id=job_id,
                outcome=Outcome.ERROR,
                at=at,
            ),
            problem=str(error),
        )
    return Classification(
        finished=AttemptFinished(
            unit=finished.unit,
            job_id=finished.job_id,
            outcome=finished.outcome,
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
    lifecycle: str = Lifecycle.CREATED

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
        classify_result(lane.unit, lane.result, at)
        for lane in lanes
        if lane.finished
    )

    after = [*records, *(item.finished for item in classified)]
    state = lifecycle(after)
    if state != Lifecycle.RUNNING:
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


class EventFamily(_StringEnum):
    """The groups a subscriber can ask for wholesale, as ``unit.*``."""

    CAMPAIGN = "campaign"
    LANE = "lane"
    UNIT = "unit"
    ANOMALY = "anomaly"


class EventKind(_StringEnum):
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
