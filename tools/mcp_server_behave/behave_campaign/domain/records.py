"""The campaign record: its types, and how they are read and written."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from behave_mcp.domain.pools import MAX_WSL_JOBS, POOL_OF, POOLS, Pool


class StringEnum(str, Enum):
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


class RecordType(StringEnum):
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


class Outcome(StringEnum):
    """What a finished job established.

    "running" is not among them: a unit is running when its latest attempt
    has no finish yet, which is a fact about the attempt rather than a
    result the job reported.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class UnitState(StringEnum):
    """A unit's condition, derived from its attempts rather than stored."""

    UNATTEMPTED = "unattempted"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class Lifecycle(StringEnum):
    """Where a campaign is in its life.

    CREATED is the absence of any lifecycle record; every other state is
    written.
    """

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETE = "complete"


WRITABLE_LIFECYCLE = (
    Lifecycle.RUNNING,
    Lifecycle.PAUSED,
    Lifecycle.CANCELLED,
    Lifecycle.COMPLETE,
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
    "lanes",
)


REPO_FIELDS = ("root", "commit", "branch", "dirty")


SCOPE_FIELDS = ("feature", "scenario", "release", "machine_type")


class CampaignError(ValueError):
    """Invalid campaign input or stored record."""


@dataclass(frozen=True, order=True)
class Unit:
    """One behave scenario for one release on one machine type."""

    feature: str
    scenario: str
    release: str
    machine_type: str

    @property
    def pool(self) -> Pool:
        return POOL_OF[self.machine_type]

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
    # may fill in each pool. Each attempt still records the source its own
    # job actually used, which is what a verification rests on.
    install_from: str = DEFAULT_INSTALL_SOURCE
    lanes: Mapping[Pool, int] = field(default_factory=dict)


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


def require_fields(
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


def text_field(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(
            "{} field '{}' must be a non-empty string".format(label, key)
        )
    return value


def _unit_from(raw: dict[str, Any], label: str) -> Unit:
    return Unit(
        feature=text_field(raw, "feature", label),
        scenario=text_field(raw, "scenario", label),
        release=text_field(raw, "release", label),
        machine_type=text_field(raw, "machine_type", label),
    )


def parse_unit(raw: Any) -> Unit:
    return _unit_from(require_fields(raw, UNIT_FIELDS, "unit"), "unit")


def validate_outcome(value: Any) -> Outcome:
    try:
        return Outcome(value)
    except ValueError:
        raise CampaignError(
            "outcome must be one of: {}".format(", ".join(OUTCOMES))
        ) from None


def parse_finished(raw: Any) -> AttemptFinished:
    """Read one completed try as a caller describes it."""
    body = require_fields(raw, FINISHED_INPUT_FIELDS, "attempt")
    return AttemptFinished(
        unit=_unit_from(body, "attempt"),
        job_id=text_field(body, "job_id", "attempt"),
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
    require_fields(body, STARTED_FIELDS, "started")
    return AttemptStarted(
        unit=_unit_from(body, "started"),
        job_id=text_field(body, "job_id", "started"),
        install_from=validate_install_source(
            text_field(body, "install_from", "started")
        ),
        at=text_field(body, "at", "started"),
    )


def _parse_finished(body: dict[str, Any]) -> AttemptFinished:
    require_fields(body, FINISHED_FIELDS, "finished")
    return AttemptFinished(
        unit=_unit_from(body, "finished"),
        job_id=text_field(body, "job_id", "finished"),
        outcome=validate_outcome(body["outcome"]),
        at=text_field(body, "at", "finished"),
    )


def _parse_campaign(body: dict[str, Any]) -> CampaignHeader:
    require_fields(body, CAMPAIGN_FIELDS, "campaign")
    repo = require_fields(body["repo"], REPO_FIELDS, "campaign repo")
    scope = require_fields(body["filters"], SCOPE_FIELDS, "campaign filters")

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
        text_field(body, "install_from", "campaign")
    )

    lanes = body["lanes"]
    if not isinstance(lanes, dict):
        raise CampaignError("campaign field 'lanes' must be an object")
    for pool, count in lanes.items():
        if pool not in POOLS:
            raise CampaignError(
                "campaign lanes pool must be one of: {}".format(
                    ", ".join(POOLS)
                )
            )
        _check_lane_count(Pool(pool), count)

    return CampaignHeader(
        at=text_field(body, "at", "campaign"),
        campaign_id=campaign_id,
        repo=RepoState(
            root=text_field(repo, "root", "campaign repo"),
            commit=repo["commit"],
            branch=repo["branch"],
            dirty=repo["dirty"],
        ),
        filters=Filters(**values),
        install_from=install_from,
        lanes={Pool(pool): count for pool, count in lanes.items()},
    )


def _check_lane_count(pool: Pool, count: Any) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise CampaignError(
            "{} lanes must be a positive integer, got {!r}".format(pool, count)
        )
    if pool == Pool.WSL and count > MAX_WSL_JOBS:
        raise CampaignError(
            "wsl lanes may not exceed {}: WSL jobs share one Windows "
            "host".format(MAX_WSL_JOBS)
        )


def resolve_lanes(
    units: Iterable[Unit], requested: Mapping[Pool, int]
) -> dict[Pool, int]:
    """The lanes a campaign over ``units`` runs with, one entry per pool.

    A pool the units draw on and the caller left out gets one lane. Lanes
    for a pool no unit draws on are an error rather than ignored, because
    they usually mean the scope is not what the caller thinks it is.
    """
    in_scope = {unit.pool for unit in units}
    stray = sorted(set(requested) - in_scope)
    if stray:
        raise CampaignError(
            "lanes were given for {}, but no unit in scope runs there".format(
                ", ".join(str(pool) for pool in stray)
            )
        )
    lanes = {}
    for pool in Pool:
        if pool not in in_scope:
            continue
        count = requested.get(pool, 1)
        _check_lane_count(pool, count)
        lanes[pool] = count
    return lanes


def parse_record(raw: Any) -> Record:
    if not isinstance(raw, dict):
        raise CampaignError("each record must be a JSON object")
    kind = raw.get("type")
    body = {key: value for key, value in raw.items() if key != "type"}
    if kind == RecordType.PLAN:
        require_fields(body, PLAN_FIELDS, "plan")
        return PlanRecord(
            unit=parse_unit({f: body[f] for f in UNIT_FIELDS}),
            at=text_field(body, "at", "plan"),
        )
    if kind == RecordType.STARTED:
        return _parse_started(body)
    if kind == RecordType.FINISHED:
        return _parse_finished(body)
    if kind == RecordType.CAMPAIGN:
        return _parse_campaign(body)
    if kind == RecordType.RETRY:
        require_fields(body, RETRY_FIELDS, "retry")
        reason = body["reason"]
        if not isinstance(reason, str):
            raise CampaignError("retry field 'reason' must be a string")
        return RetryRecord(
            unit=parse_unit({f: body[f] for f in UNIT_FIELDS}),
            at=text_field(body, "at", "retry"),
            reason=reason,
        )
    if kind == RecordType.LIFECYCLE:
        require_fields(body, LIFECYCLE_FIELDS, "lifecycle")
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
            at=text_field(body, "at", "lifecycle"),
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
            "lanes": lanes_as_dict(record.lanes),
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


def lanes_as_dict(lanes: Mapping[Pool, int]) -> dict[str, int]:
    return {pool.value: count for pool, count in lanes.items()}
