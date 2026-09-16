"""Pure domain logic for the behave MCP server.

Functions here are free of I/O side effects: they build commands, validate
inputs, and summarize behave JSON reports. Constants shared across modules
also live here.
"""

import re
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple, Sequence

from behave_mcp import parser
from behave_mcp.messages import (
    Combo,
    Dimensions,
    DimensionValue,
    ExamplesBlock,
    Failure,
    FeatureCatalogEntry,
    GroupedCount,
    JobRecord,
    ReportSummary,
    RunStatus,
    ScenarioStatus,
    ScenarioSummary,
)
from behave_mcp.parser import ALLOWED_MACHINE_TYPES

CLOUD_MACHINE_TYPES = {
    "aws.generic",
    "gcp.generic",
    "azure.generic",
    "aws.pro",
    "gcp.pro",
    "azure.pro",
}
ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR = "MCP_ALLOW_CLOUD_MACHINE_TYPES"
MAX_PARALLEL_JOBS_ENV_VAR = "MCP_MAX_PARALLEL_JOBS"
# Everything the server writes lives under one directory, one subdirectory
# per kind of artifact, so a checkout has a single place to look and a single
# thing to ignore or delete.
STATE_DIR_ENV_VAR = "MCP_STATE_DIR"
DEFAULT_STATE_DIR_NAME = ".mcp_server_behave"
JOBS_SUBDIR = "jobs"
CAMPAIGNS_SUBDIR = "campaigns"
CAMPAIGN_POLL_TIMEOUT_ENV_VAR = "MCP_CAMPAIGN_POLL_TIMEOUT"
# How long await_campaign_events may hold a request open. The real ceiling
# is the client's own request timeout, which varies per host and is not ours
# to set, so this is configurable and capped conservatively.
DEFAULT_CAMPAIGN_POLL_TIMEOUT = 60
MAX_CAMPAIGN_POLL_TIMEOUT = 120
# features/environment.py reads this exact name (UAClientBehaveConfig's
# UACLIENT_BEHAVE_ prefix + install_from) to pick where the behave
# subprocess installs ubuntu-pro-client from; it defaults to 'local' when
# unset, same as InstallFrom.LOCAL below, but we still set it explicitly so
# non-default values (e.g. 'proposed') actually reach that subprocess.
INSTALL_FROM_ENV_VAR = "UACLIENT_BEHAVE_INSTALL_FROM"


class InstallFrom(str, Enum):
    """Subset of features/util.py's InstallationSource exposed here.

    Excludes CUSTOM and PREBUILT, which require extra config (custom_ppa,
    debs_path) not exposed through this MCP interface.
    """

    LOCAL = "local"
    ARCHIVE = "archive"
    DAILY = "daily"
    STAGING = "staging"
    STABLE = "stable"
    PROPOSED = "proposed"


DEFAULT_INSTALL_FROM = (
    InstallFrom.LOCAL.value
)  # the pro client-defined default.
DEFAULT_RUNNING_TAIL_LINES = 12
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 2000
DEFAULT_LOG_CONTEXT = 3
MAX_LOG_CONTEXT = 20
DEFAULT_WAIT_TIMEOUT_SECONDS = 1800
DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 5.0
JOB_INDEX_FILE_NAME = "index.jsonl"
STDOUT_LOG_SUFFIX = "_stdout.log"
JSON_REPORT_SUFFIX = "_report.json"
METADATA_SUFFIX = "_meta.json"
DEFAULT_MAX_PARALLEL_JOBS = 1
DEFAULT_JOB_LIST_LIMIT = 20
MAX_JOB_LIST_LIMIT = 500
DEFAULT_SUMMARIZE_FAILURES_LIMIT = 200
MAX_SUMMARIZE_FAILURES_LIMIT = 2000


def to_combo(combo: parser.Combo) -> Combo:
    return Combo(release=combo.release, machine_type=combo.machine_type)


def to_examples_block(block: parser.ExamplesBlock) -> ExamplesBlock:
    return ExamplesBlock(
        name=block.name,
        tags=list(block.tags),
        combos=[to_combo(combo) for combo in block.combos],
    )


def to_scenario_summary(
    scenario: parser.ScenarioSummary,
) -> ScenarioSummary:
    return ScenarioSummary(
        name=scenario.name,
        type=scenario.type,
        tags=list(scenario.tags),
        requires_config=list(scenario.requires_config),
        example_columns=list(scenario.example_columns),
        combos=[to_combo(combo) for combo in scenario.combos],
        examples=[to_examples_block(block) for block in scenario.examples],
    )


def to_catalog_entry(
    entry: parser.FeatureCatalogEntry,
) -> FeatureCatalogEntry:
    return FeatureCatalogEntry(
        path=entry.path,
        title=entry.title,
        scenario_count=entry.scenario_count,
        requires_config=list(entry.requires_config),
        releases=list(entry.releases),
        machine_types=list(entry.machine_types),
    )


def to_dimensions(dimensions: parser.Dimensions) -> Dimensions:
    return Dimensions(
        releases=[
            DimensionValue(
                name=value.name, scenario_count=value.scenario_count
            )
            for value in dimensions.releases
        ],
        machine_types=[
            DimensionValue(
                name=value.name, scenario_count=value.scenario_count
            )
            for value in dimensions.machine_types
        ],
    )


def validate_machine_types(
    machine_types: list[str], allow_cloud: bool
) -> str | None:
    if not machine_types:
        return (
            "machine_types is required. Allowed values: lxd-container,lxd-vm"
        )

    invalid_machine_types = sorted(
        machine_type
        for machine_type in machine_types
        if machine_type not in ALLOWED_MACHINE_TYPES
    )
    if invalid_machine_types:
        return (
            "Unsupported machine_types: "
            f"{','.join(invalid_machine_types)}. "
            "Allowed values: lxd-container,lxd-vm"
        )

    cloud_machine_types = sorted(
        machine_type
        for machine_type in machine_types
        if machine_type in CLOUD_MACHINE_TYPES
    )
    if cloud_machine_types and not allow_cloud:
        return (
            "Cloud machine_types are disabled by default. "
            "Set "
            f"{ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR}=1 "
            "to allow: "
            f"{','.join(cloud_machine_types)}"
        )

    return None


def validate_install_from(install_from: str) -> str | None:
    try:
        InstallFrom(install_from)
    except ValueError:
        allowed = sorted(member.value for member in InstallFrom)
        return (
            f"Unsupported install_from: {install_from}. Allowed values: "
            f"{','.join(allowed)}"
        )
    return None


class ClassifyReason(str, Enum):
    """Machine-readable explanation for a ``classify_job_state`` outcome."""

    LIVE_HANDLE_RUNNING = "live_handle_running"
    LIVE_HANDLE_EXITED = "live_handle_exited"
    REPORT_PRESENT = "report_present"
    PID_UNKNOWN_NO_REPORT = "pid_unknown_no_report"
    PID_ALIVE_NO_REPORT = "pid_alive_no_report"
    PID_DEAD_NO_REPORT = "pid_dead_no_report"


class JobState(NamedTuple):
    """Classification of a job's state from observable signals.

    ``reason`` is a machine-readable explanation the service layer logs.
    """

    status: RunStatus
    ok: bool | None
    reason: ClassifyReason


def classify_job_state(
    *,
    has_live_handle: bool,
    returncode: int | None,
    report_present: bool,
    report_ok: bool | None,
    pid: int | None,
    pid_alive: bool,
) -> JobState:
    """Decide a job's status from process/report/pid signals.

    No I/O happens here -- callers gather ``report_present``/``report_ok``
    (from a parsed JSON report) and ``pid_alive`` (from a liveness probe)
    beforehand, so this stays a plain, exhaustively unit-testable function.
    """
    if has_live_handle:
        if returncode is None:
            return JobState(
                RunStatus.RUNNING, None, ClassifyReason.LIVE_HANDLE_RUNNING
            )
        return JobState(
            RunStatus.COMPLETED,
            returncode == 0,
            ClassifyReason.LIVE_HANDLE_EXITED,
        )

    if report_present:
        return JobState(
            RunStatus.COMPLETED,
            bool(report_ok),
            ClassifyReason.REPORT_PRESENT,
        )

    if pid is None:
        return JobState(
            RunStatus.UNKNOWN, False, ClassifyReason.PID_UNKNOWN_NO_REPORT
        )

    if pid_alive:
        return JobState(
            RunStatus.RUNNING, None, ClassifyReason.PID_ALIVE_NO_REPORT
        )

    return JobState(
        RunStatus.UNKNOWN, False, ClassifyReason.PID_DEAD_NO_REPORT
    )


def build_command(
    feature_file: str,
    machine_types: list[str],
    scenario_name: str,
    releases: list[str] | None,
    json_report_path: Path,
) -> list[str]:
    command = ["tox", "-e", "behave", "--", feature_file]
    if scenario_name:
        command.extend(["--name", scenario_name])
    if releases:
        command.extend(["-D", f"releases={','.join(releases)}"])
    command.extend(["-D", f"machine_types={','.join(machine_types)}"])
    command.extend(["-f", "json", "-o", str(json_report_path)])
    command.extend(["-f", "plain"])
    return command


class JobArtifactPaths(NamedTuple):
    """The three on-disk artifact paths a job owns, derived from its id."""

    stdout_log: Path
    json_report: Path
    metadata: Path


def job_artifact_paths(log_dir: Path, job_id: str) -> JobArtifactPaths:
    """Return a job's artifact paths -- the single naming-convention source."""
    return JobArtifactPaths(
        stdout_log=log_dir / f"{job_id}{STDOUT_LOG_SUFFIX}",
        json_report=log_dir / f"{job_id}{JSON_REPORT_SUFFIX}",
        metadata=log_dir / f"{job_id}{METADATA_SUFFIX}",
    )


class BehaveStepStatus(str, Enum):
    """A step ``result.status`` value we recognize in a behave report."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    UNDEFINED = "undefined"


class BehaveScenarioStatus(str, Enum):
    """A scenario ``status`` value we recognize in a behave report."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    HOOK_ERROR = "hook_error"
    CLEANUP_ERROR = "cleanup_error"
    UNDEFINED = "undefined"
    SKIPPED = "skipped"


_FAILING_STEP_STATUSES = frozenset(
    {
        BehaveStepStatus.FAILED,
        BehaveStepStatus.ERROR,
        BehaveStepStatus.UNDEFINED,
    }
)

_SCENARIO_STATUS_MAP = {
    BehaveScenarioStatus.PASSED: ScenarioStatus.PASSED,
    BehaveScenarioStatus.FAILED: ScenarioStatus.FAILED,
    BehaveScenarioStatus.ERROR: ScenarioStatus.FAILED,
    BehaveScenarioStatus.HOOK_ERROR: ScenarioStatus.FAILED,
    BehaveScenarioStatus.CLEANUP_ERROR: ScenarioStatus.FAILED,
    BehaveScenarioStatus.UNDEFINED: ScenarioStatus.FAILED,
    BehaveScenarioStatus.SKIPPED: ScenarioStatus.SKIPPED,
}


def _behave_step_status(value: Any) -> BehaveStepStatus | None:
    """Parse a raw step status into a recognized member, else None."""
    try:
        return BehaveStepStatus(value)
    except ValueError:
        return None


def _behave_scenario_status(value: Any) -> BehaveScenarioStatus | None:
    """Parse a raw scenario status into a recognized member, else None."""
    try:
        return BehaveScenarioStatus(value)
    except ValueError:
        return None


def scenario_status_from_steps(steps: list[dict[str, Any]]) -> ScenarioStatus:
    statuses = {
        _behave_step_status(step.get("result", {}).get("status"))
        for step in steps
    }
    if statuses & _FAILING_STEP_STATUSES:
        return ScenarioStatus.FAILED
    if statuses == {BehaveStepStatus.SKIPPED}:
        return ScenarioStatus.SKIPPED
    if BehaveStepStatus.PASSED in statuses:
        return ScenarioStatus.PASSED
    return ScenarioStatus.UNKNOWN


def scenario_status_from_element(scenario: dict[str, Any]) -> ScenarioStatus:
    """Classify a report scenario element into passed/failed/skipped/unknown.

    Prefers behave's own scenario-level ``status`` -- the JSON report
    already carries it (it's ``scenario.status.name`` from behave's model,
    the same authoritative value the JUnit reporter's ``testcase.@status``
    would give), so there's no need to infer it. Falls back to
    ``scenario_status_from_steps`` only when that key is absent (e.g.
    hand-built fixtures): a skipped scenario has no executed steps to
    infer a status from, so step-based inference alone always mis-buckets
    it as "unknown" instead of "skipped".
    """
    behave_status = _behave_scenario_status(scenario.get("status"))
    if behave_status is not None:
        return _SCENARIO_STATUS_MAP[behave_status]

    steps = scenario.get("steps", [])
    if not isinstance(steps, list):
        steps = []
    return scenario_status_from_steps(steps)


def summarize_report(report_data: list[Any]) -> ReportSummary:
    summary = {
        "features": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "scenarios": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "steps": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "error": 0,
            "undefined": 0,
            "unknown": 0,
        },
    }
    failures: list[Failure] = []

    for feature in report_data:
        feature_name = str(feature.get("name", "unknown-feature"))
        scenarios = feature.get("elements", [])
        if not isinstance(scenarios, list):
            scenarios = []

        summary["features"]["total"] += 1
        scenario_statuses: list[ScenarioStatus] = []

        for scenario in scenarios:
            scenario_name = str(scenario.get("name", "unknown-scenario"))
            steps = scenario.get("steps", [])
            if not isinstance(steps, list):
                steps = []

            scenario_status = scenario_status_from_element(scenario)
            scenario_statuses.append(scenario_status)
            summary["scenarios"]["total"] += 1
            summary["scenarios"][scenario_status] = (
                summary["scenarios"].get(scenario_status, 0) + 1
            )

            for step in steps:
                step_name = str(step.get("name", "unknown-step"))
                result = step.get("result", {})
                step_status = str(result.get("status", "unknown"))

                summary["steps"]["total"] += 1
                summary["steps"][step_status] = (
                    summary["steps"].get(step_status, 0) + 1
                )

                if _behave_step_status(step_status) in _FAILING_STEP_STATUSES:
                    error_message = str(
                        result.get("error_message", "")
                    ).strip()
                    failures.append(
                        Failure(
                            feature=feature_name,
                            scenario=scenario_name,
                            step=step_name,
                            status=step_status,
                            error_message=error_message[:2000],
                        )
                    )

        if any(
            status == ScenarioStatus.FAILED for status in scenario_statuses
        ):
            feature_status = ScenarioStatus.FAILED
        elif scenario_statuses and all(
            status == ScenarioStatus.SKIPPED for status in scenario_statuses
        ):
            feature_status = ScenarioStatus.SKIPPED
        elif any(
            status == ScenarioStatus.PASSED for status in scenario_statuses
        ):
            feature_status = ScenarioStatus.PASSED
        else:
            feature_status = ScenarioStatus.UNKNOWN

        summary["features"][feature_status] = (
            summary["features"].get(feature_status, 0) + 1
        )

    return ReportSummary(summary=summary, failures=failures)


def job_matches_result_filters(
    record: JobRecord,
    *,
    job_id: str,
    job_ids: set[str] | None,
    feature_file: str | None,
    scenario_name: str | None,
    release: str | None,
    machine_type: str | None,
) -> bool:
    """Return whether a job's record satisfies ``summarize_scenario_results``
    filters. Only ``job_ids`` inspects ``job_id`` directly; the rest read
    fields already present in that job's record.
    """
    if job_ids is not None and job_id not in job_ids:
        return False
    if feature_file is not None and record.feature_file != feature_file:
        return False
    if scenario_name is not None:
        if scenario_name.lower() not in record.scenario_name.lower():
            return False
    if release is not None and release not in record.releases:
        return False
    if machine_type is not None and machine_type not in record.machine_types:
        return False
    return True


def _empty_grouped_count() -> dict[str, Any]:
    return {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "unknown": 0,
    }


def grouped_counts_from_report(
    report_data: list[Any],
    releases: list[str],
    machine_types: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return one job's per-release and per-machine_type scenario counts.

    Every scenario in the report is attributed to all of the job's declared
    ``releases``/``machine_types`` -- a single job's Examples rows aren't
    mapped back to which specific row produced which scenario.
    """
    by_release: dict[str, dict[str, Any]] = {}
    by_machine_type: dict[str, dict[str, Any]] = {}

    for feature in report_data:
        scenarios = feature.get("elements", [])
        if not isinstance(scenarios, list):
            scenarios = []
        for scenario in scenarios:
            status = scenario_status_from_element(scenario)

            for name in releases:
                entry = by_release.setdefault(name, _empty_grouped_count())
                entry["total"] += 1
                entry[status] += 1
            for name in machine_types:
                entry = by_machine_type.setdefault(
                    name, _empty_grouped_count()
                )
                entry["total"] += 1
                entry[status] += 1

    return by_release, by_machine_type


def merge_grouped_counts(
    target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]
) -> None:
    """Merge one job's ``grouped_counts_from_report`` output into a running
    total."""
    for name, counts in source.items():
        entry = target.setdefault(name, _empty_grouped_count())
        for key in ("total", "passed", "failed", "skipped", "unknown"):
            entry[key] += counts.get(key, 0)


def grouped_counts_from_dict(
    counts: dict[str, dict[str, Any]],
) -> list[GroupedCount]:
    """Project a merged ``grouped_counts_from_report`` dict into sorted
    DTOs."""
    return [
        GroupedCount(
            name=name,
            total=data["total"],
            passed=data["passed"],
            failed=data["failed"],
            skipped=data["skipped"],
            unknown=data["unknown"],
        )
        for name, data in sorted(counts.items())
    ]


def job_failures_from_report(
    report_data: list[Any],
    job_id: str,
    releases: list[str],
    machine_types: list[str],
) -> list[Failure]:
    """Extract failing steps tagged with job/release/machine_type context.

    Mirrors ``summarize_report``'s failure extraction but attaches
    ``job_id`` and the job's declared releases/machine_types to each
    failure. Used only by ``summarize_scenario_results``;
    ``wait_for_completion``/``get_scenario_artifacts`` don't need this
    extra context.
    """
    failures: list[Failure] = []

    for feature in report_data:
        feature_name = str(feature.get("name", "unknown-feature"))
        scenarios = feature.get("elements", [])
        if not isinstance(scenarios, list):
            scenarios = []
        for scenario in scenarios:
            scenario_name = str(scenario.get("name", "unknown-scenario"))
            steps = scenario.get("steps", [])
            if not isinstance(steps, list):
                steps = []

            for step in steps:
                step_name = str(step.get("name", "unknown-step"))
                result = step.get("result", {})
                step_status = str(result.get("status", "unknown"))
                if _behave_step_status(step_status) not in (
                    _FAILING_STEP_STATUSES
                ):
                    continue
                error_message = str(result.get("error_message", "")).strip()
                failures.append(
                    Failure(
                        feature=feature_name,
                        scenario=scenario_name,
                        step=step_name,
                        status=step_status,
                        error_message=error_message[:2000],
                        job_id=job_id,
                        releases=list(releases),
                        machine_types=list(machine_types),
                    )
                )

    return failures


class LogSelection(NamedTuple):
    """Part of a log, as :func:`select_log_lines` returns it.

    ``first_line``/``last_line`` bound what was returned and are None when
    nothing was. ``matches`` counts every match from ``start`` on, whether
    or not it fit; ``truncated`` says some of the log asked for did not.
    """

    text: str
    first_line: int | None
    last_line: int | None
    matches: int
    truncated: bool


def _numbered(lines: Sequence[str], first: int, last: int) -> list[str]:
    """Lines ``first``..``last``, 1-based and inclusive, prefixed ``N: ``."""
    return [
        "{}: {}".format(number, lines[number - 1])
        for number in range(first, last + 1)
    ]


def select_log_lines(
    lines: Sequence[str],
    *,
    pattern: str = "",
    context: int = DEFAULT_LOG_CONTEXT,
    start: int = 0,
    limit: int = DEFAULT_LOG_LINES,
) -> LogSelection:
    """Pick the part of a log a caller asked for.

    With ``pattern`` (a regex, case-insensitive): every line matching it
    from ``start`` on, each with ``context`` lines either side, as grep
    would print them -- overlapping windows merge, and ``--`` separates
    the rest. Windows are taken in order until the next would push the
    output past ``limit`` lines.

    Without: ``limit`` lines from ``start``, or the last ``limit`` lines
    when ``start`` is 0.
    """
    total = len(lines)
    if not pattern:
        if start > 0:
            first = start
            last = min(total, start + limit - 1)
        else:
            first = max(1, total - limit + 1)
            last = total
        if total == 0 or first > total:
            return LogSelection("", None, None, 0, False)
        return LogSelection(
            "\n".join(_numbered(lines, first, last)),
            first,
            last,
            0,
            (last < total) if start > 0 else (first > 1),
        )

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as error:
        raise ValueError("invalid pattern {!r}: {}".format(pattern, error))

    hits = [
        number
        for number in range(max(start, 1), total + 1)
        if regex.search(lines[number - 1])
    ]
    windows: list[tuple[int, int]] = []
    budget = limit
    truncated = False
    for number in hits:
        low = max(1, number - context)
        high = min(total, number + context)
        if windows and low <= windows[-1][1] + 1:
            merged_high = max(windows[-1][1], high)
            cost = merged_high - windows[-1][1]
        else:
            cost = high - low + 1
        if cost > budget:
            truncated = True
            break
        budget -= cost
        if windows and low <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], high))
        else:
            windows.append((low, high))
    if not windows:
        return LogSelection("", None, None, len(hits), truncated)
    text = "\n--\n".join(
        "\n".join(_numbered(lines, low, high)) for low, high in windows
    )
    return LogSelection(
        text, windows[0][0], windows[-1][1], len(hits), truncated
    )


# A digest is bounded by construction: one hook error in the wild was 117
# lines of `lxc info` JSON, and a run can fail the same way many times.
DIGEST_HEAD_LINES = 25
DIGEST_TAIL_LINES = 8
DIGEST_MAX_LINE_CHARS = 400
DIGEST_MAX_REGIONS = 10
DIGEST_TAIL = 5

_STEP_RESULT = re.compile(
    r"^\s+(?:Given|When|Then|And|But) .* \.\.\. "
    r"(?:failed|error|hook_error|undefined) in [\d.]+s$"
)
_SCENARIO_START = re.compile(r"^\s+Scenario(?: Outline)?: ")
_TRACEBACK_START = "Traceback (most recent call last):"
_CHAINED = ("The above exception", "During handling of the above exception")
_HOOK_ERROR = re.compile(r"^HOOK-ERROR in \w+: ")
_ASSERT_FAILED = "ASSERT FAILED"
_SUMMARY_LIST = re.compile(r"^(?:Failing|Errored) scenarios:")
_SUMMARY_COUNTS = re.compile(r"^\d+ features? passed")
_SUMMARY_END = re.compile(r"^Took ")


class StepRef(NamedTuple):
    line: int
    text: str


class LogRegion(NamedTuple):
    """One failure as the log shows it: a traceback, hook error or assert.

    ``exception`` is the line that names what went wrong -- for a chained
    traceback, the last one raised. ``text`` is the region, capped.
    """

    kind: str
    first_line: int
    last_line: int
    step: StepRef | None
    exception: str
    text: str


class LogSummary(NamedTuple):
    first_line: int
    last_line: int
    text: str


class LogDigest(NamedTuple):
    """What a failed job's log says, in order, without reading it."""

    total_lines: int
    finished: bool
    errors: list[LogRegion]
    errors_total: int
    summary: LogSummary | None
    tail: str


def _clip(line: str) -> str:
    if len(line) <= DIGEST_MAX_LINE_CHARS:
        return line
    return line[:DIGEST_MAX_LINE_CHARS] + " …"


def _capped(lines: Sequence[str]) -> str:
    clipped = [_clip(line) for line in lines]
    if len(clipped) <= DIGEST_HEAD_LINES + DIGEST_TAIL_LINES:
        return "\n".join(clipped)
    omitted = len(clipped) - DIGEST_HEAD_LINES - DIGEST_TAIL_LINES
    return "\n".join(
        [
            *clipped[:DIGEST_HEAD_LINES],
            "... [{} lines omitted] ...".format(omitted),
            *clipped[-DIGEST_TAIL_LINES:],
        ]
    )


def _to_blank(lines: Sequence[str], index: int) -> int:
    """Index just past the block of non-blank lines starting at ``index``."""
    while index < len(lines) and lines[index].strip():
        index += 1
    return index


def _skip_blank(lines: Sequence[str], index: int) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _traceback_end(lines: Sequence[str], index: int) -> int:
    """Index just past a traceback, following chained-exception blocks.

    Python prints a chain as: exception, blank, "The above exception ...",
    blank, the next traceback. Each link is taken as part of the same
    region, since it is one failure.
    """
    end = _to_blank(lines, index)
    while True:
        marker = _skip_blank(lines, end)
        if marker >= len(lines) or not lines[marker].startswith(_CHAINED):
            return end
        following = _skip_blank(lines, marker + 1)
        if following >= len(lines) or not lines[following].startswith(
            _TRACEBACK_START
        ):
            return end
        end = _to_blank(lines, following)


def _raised(lines: Sequence[str]) -> str:
    """The line naming the exception: the first unindented line after the
    last stack frame. A multi-line message keeps only its first line."""
    last_frame = -1
    for position, line in enumerate(lines):
        if line.startswith('  File "'):
            last_frame = position
    for line in lines[last_frame + 1 :]:
        if line and not line[0].isspace():
            return _clip(line)
    return _clip(lines[-1]) if lines else ""


def digest_log(lines: Sequence[str]) -> LogDigest:
    """Pull the failures out of a behave log, in the order they happened.

    Regions are tied to the nearest failing step line above them, until a
    new scenario starts. The behave summary block and the last few lines
    come along so a run that never reached behave is still diagnosable.
    """
    total = len(lines)
    regions: list[LogRegion] = []
    summary: LogSummary | None = None
    step: StepRef | None = None
    index = 0
    while index < total:
        line = lines[index]
        if _SCENARIO_START.match(line):
            step = None
        elif _STEP_RESULT.match(line):
            step = StepRef(index + 1, line.strip())
        elif line.startswith(_TRACEBACK_START):
            end = _traceback_end(lines, index)
            block = lines[index:end]
            regions.append(
                LogRegion(
                    "traceback",
                    index + 1,
                    end,
                    step,
                    _raised(block),
                    _capped(block),
                )
            )
            index = end
            continue
        elif _HOOK_ERROR.match(line):
            end = _to_blank(lines, index)
            regions.append(
                LogRegion(
                    "hook_error",
                    index + 1,
                    end,
                    step,
                    _clip(line),
                    _capped(lines[index:end]),
                )
            )
            index = end
            continue
        elif line.startswith(_ASSERT_FAILED):
            end = index + 1
            while (
                end < total
                and lines[end].strip()
                and not lines[end].startswith(_TRACEBACK_START)
            ):
                end += 1
            exception = _clip(line)
            # behave prints the assertion, a blank, then its traceback:
            # the same failure, so one region.
            following = _skip_blank(lines, end)
            if following < total and lines[following].startswith(
                _TRACEBACK_START
            ):
                end = _traceback_end(lines, following)
                exception = _raised(lines[index:end])
            regions.append(
                LogRegion(
                    "assert",
                    index + 1,
                    end,
                    step,
                    exception,
                    _capped(lines[index:end]),
                )
            )
            index = end
            continue
        elif summary is None and (
            _SUMMARY_LIST.match(line) or _SUMMARY_COUNTS.match(line)
        ):
            end = index
            while end < total and not _SUMMARY_END.match(lines[end]):
                end += 1
            if end < total:
                summary = LogSummary(
                    index + 1, end + 1, "\n".join(lines[index : end + 1])
                )
                index = end + 1
                continue
        index += 1

    return LogDigest(
        total_lines=total,
        finished=summary is not None,
        errors=regions[:DIGEST_MAX_REGIONS],
        errors_total=len(regions),
        summary=summary,
        tail="\n".join(_clip(line) for line in lines[-DIGEST_TAIL:]),
    )
