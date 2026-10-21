"""The campaign record: its types, and how they are read and written."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from behave_mcp.domain.pools import MAX_WSL_JOBS, POOL_OF, POOLS, Pool


class StringEnum(str, Enum):
    """A string enum that reads as its value.

    ``Enum`` renders a member as ``Class.MEMBER``, which would put
    "Intent.HOLD" into error messages a person reads and JSON a caller
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
    # A job that ended without establishing an outcome, such as one killed
    # by a pause. It closes the attempt and leaves the unit queueable.
    INTERRUPTED = "interrupted"
    INTENT = "intent"
    RETRY = "retry"
    # Observations the scheduler writes once per episode, so a watcher
    # hears of each once, across restarts.
    STARVED = "starved"
    OVERDUE = "overdue"


class Outcome(StringEnum):
    """What a finished job established.

    "in flight" is not among them: a unit is in flight while an attempt has
    no finish yet, which is a fact about the attempt rather than a result
    the job reported.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class UnitState(StringEnum):
    """A unit's condition, derived from its attempts rather than stored.

    IN_FLIGHT wins over the outcome: a passed unit being retried is in
    flight until that attempt ends, and passed again after it.
    """

    UNATTEMPTED = "unattempted"
    IN_FLIGHT = "in_flight"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class Intent(StringEnum):
    """What the caller last asked a campaign to do. The only state written.

    NEVER_RUN is the absence of any intent record.
    """

    NEVER_RUN = "never_run"
    RUN = "run"
    HOLD = "hold"


WRITABLE_INTENTS = (Intent.RUN, Intent.HOLD)


class Cause(StringEnum):
    """What moved a campaign: a command, or the scheduler on its own."""

    RUN = "run"
    PAUSE = "pause"
    RETRY = "retry"
    SCHEDULER = "scheduler"


# The commands that write an intent.
COMMAND_CAUSES = (Cause.RUN, Cause.PAUSE, Cause.RETRY)


class Progress(StringEnum):
    """How much work a campaign has left, derived from its units."""

    # No unit has been attempted.
    UNTOUCHED = "untouched"
    # At least one unit can be queued.
    OPEN = "open"
    # Nothing can be queued, but a job is in flight.
    FINISHING = "finishing"
    # Nothing can be queued and nothing is in flight.
    SETTLED = "settled"


class CampaignState(StringEnum):
    """A campaign's named state: one reachable pair of intent and progress."""

    CREATED = "created"
    RUNNING = "running"
    FINISHING = "finishing"
    COMPLETE = "complete"
    PAUSING = "pausing"
    PAUSED = "paused"


# Outcomes worth another look. A unit in one of these is only ever re-run
# when someone asks.
PROBLEM_OUTCOMES = (Outcome.FAILED, Outcome.SKIPPED, Outcome.ERROR)


# Tuples of plain strings, for argparse choices and error messages.
RECORD_TYPES = tuple(kind.value for kind in RecordType)


OUTCOMES = tuple(outcome.value for outcome in Outcome)


STATES = tuple(state.value for state in UnitState)


CAMPAIGN_STATES = tuple(state.value for state in CampaignState)


INSTALL_FROM_VALUES = (
    "local",
    "archive",
    "daily",
    "staging",
    "stable",
    "proposed",
)


DEFAULT_INSTALL_FROM = INSTALL_FROM_VALUES[0]


UNIT_FIELDS = ("feature", "scenario", "release", "machine_type")


STARTED_FIELDS = (*UNIT_FIELDS, "job_id", "install_from", "at")


FINISHED_FIELDS = (
    *UNIT_FIELDS,
    "job_id",
    "outcome",
    "at",
    "reason",
    "failures",
)


FAILURE_FIELDS = ("step", "status", "error_message")


STARVED_FIELDS = (*UNIT_FIELDS, "pool", "reason", "at")


OVERDUE_FIELDS = (*UNIT_FIELDS, "job_id", "elapsed_seconds", "at")


PLAN_FIELDS = (*UNIT_FIELDS, "at")


INTERRUPTED_FIELDS = (*UNIT_FIELDS, "job_id", "at", "reason")


INTENT_FIELDS = ("intent", "cause", "at")


RETRY_FIELDS = (*UNIT_FIELDS, "at", "reason")


CAMPAIGN_FIELDS = (
    "at",
    "campaign_id",
    "repo",
    "scope",
    "install_from",
    "max_jobs",
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
class StartedRecord:
    """A job began for this unit. Written as the lane opens."""

    unit: Unit
    job_id: str
    install_from: str = ""
    at: str = ""


@dataclass(frozen=True)
class Failure:
    """One failing step, as a finished job's report gave it."""

    step: str
    status: str
    error_message: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "step": self.step,
            "status": self.status,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class FinishedRecord:
    """That job ended, and what it established.

    ``reason`` says why a result could not be classified, which makes the
    outcome ``error``. ``failures`` carries the first failing steps, so a
    failure can be judged without the job's report.
    """

    unit: Unit
    job_id: str
    outcome: Outcome
    at: str = ""
    reason: str = ""
    failures: tuple[Failure, ...] = ()


@dataclass(frozen=True)
class InterruptedRecord:
    """That job ended without an outcome; the unit may be queued again."""

    unit: Unit
    job_id: str
    at: str = ""
    reason: str = ""


@dataclass(frozen=True)
class StarvedRecord:
    """A unit could not get a lane in its pool. Once per episode: until a
    lane next opens in that pool."""

    unit: Unit
    pool: Pool
    reason: str
    at: str = ""


@dataclass(frozen=True)
class OverdueRecord:
    """A job has run longer than the scheduler expects. Once per job."""

    unit: Unit
    job_id: str
    elapsed_seconds: int
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
    interrupted: bool = False

    @property
    def in_flight(self) -> bool:
        return self.outcome is None and not self.interrupted

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "install_from": self.install_from,
            "started_at": self.started_at,
            "outcome": self.outcome,
            "finished_at": self.finished_at,
            "interrupted": self.interrupted,
        }


@dataclass(frozen=True)
class UnitHistory:
    unit: Unit
    state: str
    job_id: str | None
    attempts: tuple[Attempt, ...]
    # Someone asked for another go at this unit since its last attempt. The
    # state still reports what actually happened; this is only about what
    # the scheduler may pick up.
    retry_pending: bool = False

    @property
    def in_flight(self) -> bool:
        return self.state == UnitState.IN_FLIGHT

    @property
    def queueable(self) -> bool:
        """Whether a scheduler may start an attempt at this unit.

        A problem unit is not queueable on its own: judging a failure
        flaky or real is for whoever is watching, so it waits for a retry.
        """
        if self.in_flight:
            return False
        if not self.attempts or self.retry_pending:
            return True
        # The attempt asked for never happened, so it is still owed.
        return self.attempts[-1].interrupted

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
class Scope:
    """The units a campaign was created over. Empty means unrestricted."""

    feature: tuple[str, ...] = ()
    scenario: tuple[str, ...] = ()
    release: tuple[str, ...] = ()
    machine_type: tuple[str, ...] = ()

    def matches_unit(self, unit: Unit) -> bool:
        return (
            (not self.feature or unit.feature in self.feature)
            and (not self.scenario or unit.scenario in self.scenario)
            and (not self.release or unit.release in self.release)
            and (
                not self.machine_type or unit.machine_type in self.machine_type
            )
        )

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "feature": list(self.feature),
            "scenario": list(self.scenario),
            "release": list(self.release),
            "machine_type": list(self.machine_type),
        }


@dataclass(frozen=True)
class Filters(Scope):
    """What a list query narrows units by: a scope, and unit states."""

    state: tuple[str, ...] = ()

    def matches(self, status: UnitHistory) -> bool:
        return self.matches_unit(status.unit) and (
            not self.state or status.state in self.state
        )


@dataclass(frozen=True)
class CampaignRecord:
    """How a campaign was created, so it can be recreated and audited."""

    at: str = ""
    campaign_id: str | None = None
    repo: RepoState = RepoState()
    scope: Scope = Scope()
    # The install source every job runs with, and how many of its jobs may
    # be in flight at once in each pool. Each attempt still records the
    # source its own job actually used, which is what a verification rests
    # on.
    install_from: str = DEFAULT_INSTALL_FROM
    max_jobs: Mapping[Pool, int] = field(default_factory=dict)


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
class IntentRecord:
    """What a caller asked the campaign to do: run, or hold."""

    intent: Intent
    cause: Cause
    at: str = ""


Record = (
    CampaignRecord
    | PlanRecord
    | StartedRecord
    | FinishedRecord
    | InterruptedRecord
    | IntentRecord
    | RetryRecord
    | StarvedRecord
    | OverdueRecord
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


def validate_install_from(value: str) -> str:
    if value not in INSTALL_FROM_VALUES:
        raise CampaignError(
            "install source must be one of: {}".format(
                ", ".join(INSTALL_FROM_VALUES)
            )
        )
    return value


def _parse_started(body: dict[str, Any]) -> StartedRecord:
    require_fields(body, STARTED_FIELDS, "started")
    return StartedRecord(
        unit=_unit_from(body, "started"),
        job_id=text_field(body, "job_id", "started"),
        install_from=validate_install_from(
            text_field(body, "install_from", "started")
        ),
        at=text_field(body, "at", "started"),
    )


def _parse_finished(body: dict[str, Any]) -> FinishedRecord:
    require_fields(body, FINISHED_FIELDS, "finished")
    failures = body["failures"]
    if not isinstance(failures, list):
        raise CampaignError("finished field 'failures' must be a list")
    parsed = []
    for raw in failures:
        failure = require_fields(raw, FAILURE_FIELDS, "failure")
        if not all(isinstance(failure[f], str) for f in FAILURE_FIELDS):
            raise CampaignError("failure fields must be strings")
        parsed.append(Failure(**failure))
    return FinishedRecord(
        unit=_unit_from(body, "finished"),
        job_id=text_field(body, "job_id", "finished"),
        outcome=validate_outcome(body["outcome"]),
        at=text_field(body, "at", "finished"),
        reason=_string_field(body, "reason", "finished"),
        failures=tuple(parsed),
    )


def _parse_starved(body: dict[str, Any]) -> StarvedRecord:
    require_fields(body, STARVED_FIELDS, "starved")
    pool = body["pool"]
    if pool not in POOLS:
        raise CampaignError(
            "starved pool must be one of: {}".format(", ".join(POOLS))
        )
    return StarvedRecord(
        unit=_unit_from(body, "starved"),
        pool=Pool(pool),
        reason=_string_field(body, "reason", "starved"),
        at=text_field(body, "at", "starved"),
    )


def _parse_overdue(body: dict[str, Any]) -> OverdueRecord:
    require_fields(body, OVERDUE_FIELDS, "overdue")
    elapsed = body["elapsed_seconds"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, int):
        raise CampaignError("overdue field 'elapsed_seconds' must be an int")
    return OverdueRecord(
        unit=_unit_from(body, "overdue"),
        job_id=text_field(body, "job_id", "overdue"),
        elapsed_seconds=elapsed,
        at=text_field(body, "at", "overdue"),
    )


def _parse_interrupted(body: dict[str, Any]) -> InterruptedRecord:
    require_fields(body, INTERRUPTED_FIELDS, "interrupted")
    return InterruptedRecord(
        unit=_unit_from(body, "interrupted"),
        job_id=text_field(body, "job_id", "interrupted"),
        at=text_field(body, "at", "interrupted"),
        reason=_reason_field(body, "interrupted"),
    )


def _reason_field(body: dict[str, Any], label: str) -> str:
    return _string_field(body, "reason", label)


def _string_field(body: dict[str, Any], key: str, label: str) -> str:
    value = body[key]
    if not isinstance(value, str):
        raise CampaignError(
            "{} field '{}' must be a string".format(label, key)
        )
    return value


def _parse_campaign(body: dict[str, Any]) -> CampaignRecord:
    require_fields(body, CAMPAIGN_FIELDS, "campaign")
    repo = require_fields(body["repo"], REPO_FIELDS, "campaign repo")
    scope = require_fields(body["scope"], SCOPE_FIELDS, "campaign scope")

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
                "campaign scope '{}' must be a list of strings".format(name)
            )
        values[name] = tuple(scope[name])

    install_from = validate_install_from(
        text_field(body, "install_from", "campaign")
    )

    max_jobs = body["max_jobs"]
    if not isinstance(max_jobs, dict):
        raise CampaignError("campaign field 'max_jobs' must be an object")
    for pool, count in max_jobs.items():
        if pool not in POOLS:
            raise CampaignError(
                "campaign max_jobs pool must be one of: {}".format(
                    ", ".join(POOLS)
                )
            )
        _check_max_jobs(Pool(pool), count)

    return CampaignRecord(
        at=text_field(body, "at", "campaign"),
        campaign_id=campaign_id,
        repo=RepoState(
            root=text_field(repo, "root", "campaign repo"),
            commit=repo["commit"],
            branch=repo["branch"],
            dirty=repo["dirty"],
        ),
        scope=Scope(**values),
        install_from=install_from,
        max_jobs={Pool(pool): count for pool, count in max_jobs.items()},
    )


def _check_max_jobs(pool: Pool, count: Any) -> None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise CampaignError(
            "{} max jobs must be a positive integer, got {!r}".format(
                pool, count
            )
        )
    if pool == Pool.WSL and count > MAX_WSL_JOBS:
        raise CampaignError(
            "wsl max jobs may not exceed {}: WSL jobs share one Windows "
            "host".format(MAX_WSL_JOBS)
        )


def resolve_max_jobs(
    units: Iterable[Unit], requested: Mapping[Pool, int]
) -> dict[Pool, int]:
    """A campaign's max jobs over ``units``, one entry per pool.

    A pool the units draw on and the caller left out gets 1. Max jobs for a
    pool no unit draws on are an error rather than ignored, because they
    usually mean the scope is not what the caller thinks it is.
    """
    in_scope = {unit.pool for unit in units}
    stray = sorted(set(requested) - in_scope)
    if stray:
        raise CampaignError(
            "max_jobs was given for {}, but no unit in scope runs "
            "there".format(", ".join(str(pool) for pool in stray))
        )
    max_jobs = {}
    for pool in Pool:
        if pool not in in_scope:
            continue
        count = requested.get(pool, 1)
        _check_max_jobs(pool, count)
        max_jobs[pool] = count
    return max_jobs


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
    if kind == RecordType.INTERRUPTED:
        return _parse_interrupted(body)
    if kind == RecordType.STARVED:
        return _parse_starved(body)
    if kind == RecordType.OVERDUE:
        return _parse_overdue(body)
    if kind == RecordType.CAMPAIGN:
        return _parse_campaign(body)
    if kind == RecordType.RETRY:
        require_fields(body, RETRY_FIELDS, "retry")
        return RetryRecord(
            unit=parse_unit({f: body[f] for f in UNIT_FIELDS}),
            at=text_field(body, "at", "retry"),
            reason=_reason_field(body, "retry"),
        )
    if kind == RecordType.INTENT:
        require_fields(body, INTENT_FIELDS, "intent")
        intent = body["intent"]
        if intent not in WRITABLE_INTENTS:
            raise CampaignError(
                "intent must be one of: {}".format(", ".join(WRITABLE_INTENTS))
            )
        cause = body["cause"]
        if cause not in COMMAND_CAUSES:
            raise CampaignError(
                "intent cause must be one of: {}".format(
                    ", ".join(COMMAND_CAUSES)
                )
            )
        return IntentRecord(
            intent=Intent(intent),
            cause=Cause(cause),
            at=text_field(body, "at", "intent"),
        )
    raise CampaignError(
        "record type must be one of: {}".format(", ".join(RECORD_TYPES))
    )


def encode_record(record: Record) -> dict[str, Any]:
    if isinstance(record, CampaignRecord):
        return {
            "type": RecordType.CAMPAIGN,
            "at": record.at,
            "campaign_id": record.campaign_id,
            "repo": record.repo.as_dict(),
            "scope": record.scope.as_dict(),
            "install_from": record.install_from,
            "max_jobs": max_jobs_as_dict(record.max_jobs),
        }

    if isinstance(record, IntentRecord):
        return {
            "type": RecordType.INTENT,
            "intent": record.intent,
            "cause": record.cause,
            "at": record.at,
        }

    if isinstance(record, RetryRecord):
        retry: dict[str, Any] = record.unit.as_dict()
        retry["type"] = RecordType.RETRY
        retry["at"] = record.at
        retry["reason"] = record.reason
        return retry

    data: dict[str, Any] = record.unit.as_dict()
    data["at"] = record.at
    if isinstance(record, StartedRecord):
        data["type"] = RecordType.STARTED
        data["job_id"] = record.job_id
        data["install_from"] = record.install_from
        return data
    if isinstance(record, FinishedRecord):
        data["type"] = RecordType.FINISHED
        data["job_id"] = record.job_id
        data["outcome"] = record.outcome
        data["reason"] = record.reason
        data["failures"] = [failure.as_dict() for failure in record.failures]
        return data
    if isinstance(record, StarvedRecord):
        data["type"] = RecordType.STARVED
        data["pool"] = record.pool.value
        data["reason"] = record.reason
        return data
    if isinstance(record, OverdueRecord):
        data["type"] = RecordType.OVERDUE
        data["job_id"] = record.job_id
        data["elapsed_seconds"] = record.elapsed_seconds
        return data
    if isinstance(record, InterruptedRecord):
        data["type"] = RecordType.INTERRUPTED
        data["job_id"] = record.job_id
        data["reason"] = record.reason
        return data
    data["type"] = RecordType.PLAN
    return data


def max_jobs_as_dict(max_jobs: Mapping[Pool, int]) -> dict[str, int]:
    return {pool.value: count for pool, count in max_jobs.items()}
