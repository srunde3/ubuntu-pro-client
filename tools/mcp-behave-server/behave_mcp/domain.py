"""Pure domain logic for the behave MCP server.

Functions here are free of I/O side effects: they build commands, validate
inputs, and summarize behave JSON reports. Constants shared across modules
also live here.
"""

import json
from pathlib import Path
from typing import Any, NamedTuple

from features.behave_features import ALLOWED_MACHINE_TYPES

from behave_mcp.messages import Artifacts, Failure, GroupedCount, ReportSummary

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
_DEFAULT_RUNNING_TAIL_LINES = 12
_DEFAULT_LOG_TAIL_LINES = 200
_MAX_LOG_TAIL_LINES = 2000
_DEFAULT_WAIT_TIMEOUT_SECONDS = 1800
_DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 5.0
_JOB_INDEX_FILE_NAME = "index.jsonl"
_DEFAULT_MAX_PARALLEL_JOBS = 1
_DEFAULT_JOB_LIST_LIMIT = 20
_MAX_JOB_LIST_LIMIT = 500
_DEFAULT_SUMMARIZE_FAILURES_LIMIT = 200
_MAX_SUMMARIZE_FAILURES_LIMIT = 2000


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


class JobStatus(NamedTuple):
    """Pure classification of a job's status from observable signals.

    ``reason`` is a machine-readable explanation the service layer logs but
    never needs to unit test beyond this function's own assertions.
    """

    status: str  # "running" | "completed" | "unknown"
    ok: bool | None
    reason: str


def classify_job_status(
    *,
    has_live_handle: bool,
    returncode: int | None,
    report_present: bool,
    report_ok: bool | None,
    pid: int | None,
    pid_alive: bool,
) -> JobStatus:
    """Decide a job's status from process/report/pid signals.

    No I/O happens here -- callers gather ``report_present``/``report_ok``
    (from a parsed JSON report) and ``pid_alive`` (from a liveness probe)
    beforehand, so this stays a plain, exhaustively unit-testable function.
    """
    if has_live_handle:
        if returncode is None:
            return JobStatus("running", None, "live_handle_running")
        return JobStatus("completed", returncode == 0, "live_handle_exited")

    if report_present:
        return JobStatus("completed", bool(report_ok), "report_present")

    if pid is None:
        return JobStatus("unknown", False, "pid_unknown_no_report")

    if pid_alive:
        return JobStatus("running", None, "pid_alive_no_report")

    return JobStatus("unknown", False, "pid_dead_no_report")


def build_command(
    feature_file: str,
    machine_types: list[str],
    scenario_name: str,
    releases: list[str] | None,
    json_report_path: Path,
    combo_report_path: Path,
) -> list[str]:
    command = ["tox", "-e", "behave", "--", feature_file]
    if scenario_name:
        command.extend(["--name", scenario_name])
    if releases:
        command.extend(["-D", f"releases={','.join(releases)}"])
    command.extend(["-D", f"machine_types={','.join(machine_types)}"])
    command.extend(["-f", "json", "-o", str(json_report_path)])
    command.extend(
        [
            "-f",
            "features.behave_combo_formatter:ComboFormatter",
            "-o",
            str(combo_report_path),
        ]
    )
    command.extend(["-f", "plain"])
    return command


def artifacts_payload(
    *,
    log_dir: Path,
    stdout_log: Path,
    json_report: Path,
    metadata: Path,
) -> Artifacts:
    return Artifacts(
        log_dir=str(log_dir),
        stdout_log=str(stdout_log),
        json_report=str(json_report),
        metadata=str(metadata),
    )


def scenario_status_from_steps(steps: list[dict[str, Any]]) -> str:
    statuses = {
        str(step.get("result", {}).get("status", "unknown")) for step in steps
    }
    if statuses & {"failed", "error", "undefined"}:
        return "failed"
    if statuses == {"skipped"}:
        return "skipped"
    if "passed" in statuses:
        return "passed"
    return "unknown"


_SCENARIO_STATUS_MAP = {
    "passed": "passed",
    "failed": "failed",
    "error": "failed",
    "hook_error": "failed",
    "cleanup_error": "failed",
    "undefined": "failed",
    "skipped": "skipped",
}


def scenario_status_from_element(scenario: dict[str, Any]) -> str:
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
        scenario_statuses: list[str] = []

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

                if step_status in {"failed", "error", "undefined"}:
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

        if any(status == "failed" for status in scenario_statuses):
            feature_status = "failed"
        elif scenario_statuses and all(
            status == "skipped" for status in scenario_statuses
        ):
            feature_status = "skipped"
        elif any(status == "passed" for status in scenario_statuses):
            feature_status = "passed"
        else:
            feature_status = "unknown"

        summary["features"][feature_status] = (
            summary["features"].get(feature_status, 0) + 1
        )

    return ReportSummary(summary=summary, failures=failures)


def job_matches_result_filters(
    metadata: dict[str, Any],
    *,
    job_id: str,
    job_ids: set[str] | None,
    feature_file: str | None,
    scenario_name: str | None,
    release: str | None,
    machine_type: str | None,
) -> bool:
    """Return whether a job's metadata satisfies ``summarize_scenario_results``
    filters. Only ``job_ids`` inspects ``job_id`` directly; the rest read
    fields already present in that job's ``_meta.json``.
    """
    if job_ids is not None and job_id not in job_ids:
        return False
    if (
        feature_file is not None
        and metadata.get("feature_file") != feature_file
    ):
        return False
    if scenario_name is not None:
        stored_name = str(metadata.get("scenario_name", ""))
        if scenario_name.lower() not in stored_name.lower():
            return False
    if release is not None and release not in (metadata.get("releases") or []):
        return False
    if machine_type is not None and machine_type not in (
        metadata.get("machine_types") or []
    ):
        return False
    return True


def resolve_scenario_combo(
    location: str, combo_map: dict[str, Any]
) -> tuple[str | None, str | None, bool]:
    """Resolve ``(release, machine_type, precise)`` for a report element.

    ``location`` is behave's ``"path:line"`` string for a scenario element;
    ``combo_map`` is built by ``parse_combo_report`` from that same job's
    ``ComboFormatter`` output, keyed by that identical ``location`` string.
    Returns ``(None, None, False)`` when unresolved -- e.g. a job whose combo
    report is missing/empty, or a scenario the formatter never saw.
    """
    entry = combo_map.get(location)
    if not isinstance(entry, dict):
        return None, None, False
    release = entry.get("release")
    machine_type = entry.get("machine_type")
    if not release or not machine_type:
        return None, None, False
    return str(release), str(machine_type), True


def parse_combo_report(lines: list[str]) -> dict[str, dict[str, Any]]:
    """Parse ``ComboFormatter``'s JSONL output into a location -> combo map.

    Malformed or incomplete lines are skipped rather than raised on, since
    this reads a companion artifact to the JSON report, not the report
    behave's own exit code already validated.
    """
    combo_map: dict[str, dict[str, Any]] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        location = record.get("location")
        release = record.get("release")
        machine_type = record.get("machine_type")
        if not location or not release or not machine_type:
            continue
        combo_map[str(location)] = {
            "release": release,
            "machine_type": machine_type,
        }
    return combo_map


def _empty_grouped_count() -> dict[str, Any]:
    return {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "unknown": 0,
        "precise": True,
    }


def combo_group_counts(
    report_data: list[Any],
    combo_map: dict[str, Any],
    fallback_releases: list[str],
    fallback_machine_types: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return one job's per-release and per-machine_type scenario counts.

    Each returned dict maps a release/machine_type name to counts plus a
    ``precise`` flag (see ``resolve_scenario_combo``). When a scenario's
    combo can't be resolved, its result is attributed to every one of the
    job's declared releases/machine_types instead (coarse fallback), with
    ``precise`` set to False for those entries.
    """
    by_release: dict[str, dict[str, Any]] = {}
    by_machine_type: dict[str, dict[str, Any]] = {}

    for feature in report_data:
        scenarios = feature.get("elements", [])
        if not isinstance(scenarios, list):
            scenarios = []
        for scenario in scenarios:
            status = scenario_status_from_element(scenario)
            location = str(scenario.get("location", ""))
            release, machine_type, precise = resolve_scenario_combo(
                location, combo_map
            )

            release_names: list[str]
            machine_type_names: list[str]
            if precise and release is not None and machine_type is not None:
                release_names = [release]
                machine_type_names = [machine_type]
            else:
                precise = False
                release_names = list(fallback_releases)
                machine_type_names = list(fallback_machine_types)

            for name in release_names:
                entry = by_release.setdefault(name, _empty_grouped_count())
                entry["total"] += 1
                entry[status] += 1
                entry["precise"] = entry["precise"] and precise
            for name in machine_type_names:
                entry = by_machine_type.setdefault(
                    name, _empty_grouped_count()
                )
                entry["total"] += 1
                entry[status] += 1
                entry["precise"] = entry["precise"] and precise

    return by_release, by_machine_type


def merge_combo_group_counts(
    target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]
) -> None:
    """Merge one job's ``combo_group_counts`` output into a running total."""
    for name, counts in source.items():
        entry = target.setdefault(name, _empty_grouped_count())
        for key in ("total", "passed", "failed", "skipped", "unknown"):
            entry[key] += counts.get(key, 0)
        entry["precise"] = entry["precise"] and bool(
            counts.get("precise", True)
        )


def grouped_counts_from_dict(
    counts: dict[str, dict[str, Any]],
) -> list[GroupedCount]:
    """Project a merged ``combo_group_counts`` dict into sorted DTOs."""
    return [
        GroupedCount(
            name=name,
            total=data["total"],
            passed=data["passed"],
            failed=data["failed"],
            skipped=data["skipped"],
            unknown=data["unknown"],
            precise=data["precise"],
        )
        for name, data in sorted(counts.items())
    ]


def combo_failures_from_report(
    report_data: list[Any],
    job_id: str,
    combo_map: dict[str, Any],
    fallback_releases: list[str],
    fallback_machine_types: list[str],
) -> list[Failure]:
    """Extract failing steps tagged with job/combo context.

    Mirrors ``summarize_report``'s failure extraction but attaches
    ``job_id`` and the resolved (or, if unresolved, job-declared)
    release/machine_type context to each failure. Used only by
    ``summarize_scenario_results``; ``summarize_report`` stays untouched
    since ``wait_for_completion``/``get_scenario_artifacts`` don't need
    this extra context.
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
            location = str(scenario.get("location", ""))
            release, machine_type, precise = resolve_scenario_combo(
                location, combo_map
            )
            releases = (
                [release] if precise and release else list(fallback_releases)
            )
            machine_types = (
                [machine_type]
                if precise and machine_type
                else list(fallback_machine_types)
            )

            for step in steps:
                step_name = str(step.get("name", "unknown-step"))
                result = step.get("result", {})
                step_status = str(result.get("status", "unknown"))
                if step_status not in {"failed", "error", "undefined"}:
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
                        releases=releases,
                        machine_types=machine_types,
                        precise=precise,
                    )
                )

    return failures
