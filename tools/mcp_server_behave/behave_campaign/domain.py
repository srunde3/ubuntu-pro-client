"""Pure campaign rules: test units, attempts, and what to run next.

A unit is one behave scenario for one release on one machine type. This
module owns which units are in scope, what each attempt established, and the
order remaining work is taken up in. No file or process I/O lives here, so
the rules stay directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

PLAN = "plan"
ATTEMPT = "attempt"
CAMPAIGN = "campaign"
STATE = "state"

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
UNIT_FIELDS = ("feature", "scenario", "release", "machine_type")
ATTEMPT_INPUT_FIELDS = (*UNIT_FIELDS, "state", "job_id")
ATTEMPT_FIELDS = (*ATTEMPT_INPUT_FIELDS, "install_from", "at")
PLAN_FIELDS = (*UNIT_FIELDS, "at")
STATE_FIELDS = ("state", "at", "reason")
CAMPAIGN_FIELDS = ("at", "campaign_id", "repo", "filters")
# Written only when set, and tolerated when absent, so a campaign file
# created before these existed still replays. The MCP sets both; the CLI
# leaves them unset.
CAMPAIGN_OPTIONAL_FIELDS = ("install_from", "max_lanes")
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

    def as_dict(self, include_attempts: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = self.unit.as_dict()
        data["state"] = self.state
        data["job_id"] = self.job_id
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
    # Set when the MCP created the campaign: the install source every job
    # runs with, and how many lanes the scheduler may fill. Each attempt
    # still records the source its own job actually used.
    install_from: str | None = None
    max_lanes: int | None = None


@dataclass(frozen=True)
class StateRecord:
    """A lifecycle transition someone asked for.

    ``reason`` explains transitions the server made on its own, such as
    pausing after a restart, and is empty for ones a caller asked for.
    """

    state: str
    at: str = ""
    reason: str = ""


Record = CampaignHeader | PlanRecord | AttemptRecord | StateRecord


def _require_fields(
    raw: Any,
    expected: Sequence[str],
    label: str,
    optional: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CampaignError("each {} must be a JSON object".format(label))
    missing = sorted(set(expected) - set(raw))
    unknown = sorted(set(raw) - set(expected) - set(optional))
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
        {field: body[field] for field in ATTEMPT_INPUT_FIELDS}
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
    _require_fields(
        body, CAMPAIGN_FIELDS, "campaign", CAMPAIGN_OPTIONAL_FIELDS
    )
    repo = _require_fields(body["repo"], REPO_FIELDS, "campaign repo")
    scope = _require_fields(body["filters"], SCOPE_FIELDS, "campaign filters")

    campaign_id = body["campaign_id"]
    if campaign_id is not None and not isinstance(campaign_id, str):
        raise CampaignError(
            "campaign field 'campaign_id' must be a string or null"
        )
    for field in ("commit", "branch"):
        if repo[field] is not None and not isinstance(repo[field], str):
            raise CampaignError(
                "campaign repo '{}' must be a string or null".format(field)
            )
    if repo["dirty"] is not None and not isinstance(repo["dirty"], bool):
        raise CampaignError("campaign repo 'dirty' must be a boolean or null")

    values: dict[str, tuple[str, ...]] = {}
    for field in SCOPE_FIELDS:
        if not isinstance(scope[field], list) or not all(
            isinstance(item, str) for item in scope[field]
        ):
            raise CampaignError(
                "campaign filter '{}' must be a list of strings".format(field)
            )
        values[field] = tuple(scope[field])

    install_from = body.get("install_from")
    if install_from is not None:
        validate_install_source(install_from)

    max_lanes = body.get("max_lanes")
    if max_lanes is not None and (
        isinstance(max_lanes, bool)
        or not isinstance(max_lanes, int)
        or max_lanes < 1
    ):
        raise CampaignError(
            "campaign field 'max_lanes' must be a positive integer or absent"
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
        "record type must be one of: {}, {}, {}, {}".format(
            CAMPAIGN, PLAN, ATTEMPT, STATE
        )
    )


def encode_record(record: Record) -> dict[str, Any]:
    if isinstance(record, CampaignHeader):
        header: dict[str, Any] = {
            "type": CAMPAIGN,
            "at": record.at,
            "campaign_id": record.campaign_id,
            "repo": record.repo.as_dict(),
            "filters": record.filters.scope_as_dict(),
        }
        # Omitted when unset so a CLI-created header stays byte-identical
        # to one written before these fields existed.
        if record.install_from is not None:
            header["install_from"] = record.install_from
        if record.max_lanes is not None:
            header["max_lanes"] = record.max_lanes
        return header

    if isinstance(record, StateRecord):
        return {
            "type": STATE,
            "state": record.state,
            "at": record.at,
            "reason": record.reason,
        }

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
    for record in records:
        if isinstance(record, (CampaignHeader, StateRecord)):
            continue
        if isinstance(record, PlanRecord):
            if record.unit not in seen:
                seen.add(record.unit)
                planned.append(record.unit)
        else:
            attempts.setdefault(record.unit, []).append(record)

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


def select_unattempted(
    statuses: Sequence[UnitStatus], limit: int
) -> list[UnitStatus]:
    """Units never yet attempted, for a scheduler to open lanes for.

    Unlike ``select_next``, this never returns a failed, skipped or errored
    unit. The scheduler runs unattended, and re-running a non-passing unit
    is a judgement call -- flaky or real, worth the machine time or not --
    that belongs to whoever is watching, not to the tick loop.
    """
    if limit < 1:
        raise CampaignError("limit must be positive")
    return [s for s in statuses if s.state == UNATTEMPTED][:limit]


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
        if status.state in (UNATTEMPTED, "running")
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


def classify_result(unit: Unit, result: Any, at: str = "") -> Classification:
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
                unit=unit, state="error", job_id=job_id, at=at
            ),
            problem=str(error),
        )
    return Classification(
        attempt=AttemptRecord(
            unit=attempt.unit,
            state=attempt.state,
            job_id=attempt.job_id,
            at=at,
        )
    )


@dataclass(frozen=True)
class Lane:
    """One unit occupying a lane, and its job's result if it has finished."""

    unit: Unit
    job_id: str
    result: Any = None

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
        classify_result(lane.unit, lane.result, at)
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

    # Only unattempted units: a scheduler never retries a problem unit on
    # its own. select_unattempted also skips units already running, and
    # every busy lane recorded a running attempt when it opened, so a unit
    # cannot enter two lanes.
    return TickPlan(
        record=classified,
        start=tuple(
            status.unit
            for status in select_unattempted(reduce_units(after), free)
        ),
        lifecycle=state,
    )
