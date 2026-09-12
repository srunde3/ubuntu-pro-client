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
CAMPAIGN_FIELDS = ("at", "campaign_id", "repo", "filters")
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


Record = CampaignHeader | PlanRecord | AttemptRecord


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
    _require_fields(body, CAMPAIGN_FIELDS, "campaign")
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
    raise CampaignError(
        "record type must be one of: {}, {}, {}".format(
            CAMPAIGN, PLAN, ATTEMPT
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
        if isinstance(record, CampaignHeader):
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
