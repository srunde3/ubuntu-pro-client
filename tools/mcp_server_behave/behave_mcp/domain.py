"""Pure domain logic for the behave MCP server.

Functions here are free of I/O side effects: they build commands, validate
inputs, and summarize behave JSON reports. Constants shared across modules
also live here.
"""

from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

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
DEFAULT_RUNNING_TAIL_LINES = 12
DEFAULT_LOG_TAIL_LINES = 200
MAX_LOG_TAIL_LINES = 2000
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


_FAILING_STEP_STATUSES = frozenset({"failed", "error", "undefined"})


def scenario_status_from_steps(steps: list[dict[str, Any]]) -> ScenarioStatus:
    statuses = {
        str(step.get("result", {}).get("status", "unknown")) for step in steps
    }
    if statuses & _FAILING_STEP_STATUSES:
        return ScenarioStatus.FAILED
    if statuses == {"skipped"}:
        return ScenarioStatus.SKIPPED
    if "passed" in statuses:
        return ScenarioStatus.PASSED
    return ScenarioStatus.UNKNOWN


_SCENARIO_STATUS_MAP = {
    "passed": ScenarioStatus.PASSED,
    "failed": ScenarioStatus.FAILED,
    "error": ScenarioStatus.FAILED,
    "hook_error": ScenarioStatus.FAILED,
    "cleanup_error": ScenarioStatus.FAILED,
    "undefined": ScenarioStatus.FAILED,
    "skipped": ScenarioStatus.SKIPPED,
}


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
    reported_status = scenario.get("status")
    if (
        isinstance(reported_status, str)
        and reported_status in _SCENARIO_STATUS_MAP
    ):
        return _SCENARIO_STATUS_MAP[reported_status]

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

                if step_status in _FAILING_STEP_STATUSES:
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
                if step_status not in _FAILING_STEP_STATUSES:
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
