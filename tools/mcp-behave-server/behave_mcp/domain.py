"""Pure domain logic for the behave MCP server.

Functions here are free of I/O side effects: they build commands, validate
inputs, and summarize behave JSON reports. Constants shared across modules
also live here.
"""

from pathlib import Path
from typing import Any, NamedTuple

from features.behave_features import ALLOWED_MACHINE_TYPES

from behave_mcp.messages import Artifacts, Failure, ReportSummary

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
) -> list[str]:
    command = ["tox", "-e", "behave", "--", feature_file]
    if scenario_name:
        command.extend(["--name", scenario_name])
    if releases:
        command.extend(["-D", f"releases={','.join(releases)}"])
    command.extend(["-D", f"machine_types={','.join(machine_types)}"])
    command.extend(["-f", "json", "-o", str(json_report_path), "-f", "plain"])
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

            scenario_status = scenario_status_from_steps(steps)
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
