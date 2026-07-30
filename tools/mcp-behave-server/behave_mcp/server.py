import json
import os
import posixpath
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from mcp.server import FastMCP
from starlette.responses import JSONResponse

host = os.environ.get("MCP_HOST", "127.0.0.1")
port = int(os.environ.get("MCP_PORT", "8000"))
mcp = FastMCP("Ubuntu Pro Client Behave MCP", host=host, port=port)
ALLOWED_MACHINE_TYPES = {
    "lxd-container",
    "lxd-vm",
    "aws.generic",
    "gcp.generic",
    "azure.generic",
    "aws.pro",
    "gcp.pro",
    "azure.pro",
}
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
ACTIVE_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_DEFAULT_RUNNING_TAIL_LINES = 12
_DEFAULT_LOG_TAIL_LINES = 200
_MAX_LOG_TAIL_LINES = 2000
_DEFAULT_WAIT_TIMEOUT_SECONDS = 1800
_DEFAULT_WAIT_POLL_INTERVAL_SECONDS = 5.0
_JOB_INDEX_FILE_NAME = "index.jsonl"
_DEFAULT_MAX_PARALLEL_JOBS = 1


class ErrorPayload(TypedDict):
    ok: Literal[False]
    error: str


class CapacityPayload(TypedDict):
    max_parallel_jobs: int
    running_jobs: int


class CapacityExceededPayload(TypedDict):
    ok: Literal[False]
    status: Literal["capacity_exceeded"]
    error: str
    capacity: CapacityPayload


CapacityReservationError = ErrorPayload | CapacityExceededPayload


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthcheck(request):
    return JSONResponse({"status": "ok"})


@mcp.tool(
    description=(
        "List feature files available in the repository so an agent can choose "
        "an allowed behave scenario."
    )
)
def list_features(repo_root: str = "") -> str:
    try:
        resolved_repo_root = resolve_repo_root(repo_root or None)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc), "features": []})

    return json.dumps(
        {
            "ok": True,
            "repo_root": str(resolved_repo_root),
            "features": _discover_feature_files(resolved_repo_root),
        }
    )


@mcp.tool(
    description=(
        "Start a listed behave scenario through tox in the background and "
        "return a job_id immediately. Call wait_for_scenario_completion to "
        "wait for completion."
    )
)
def start_behave_scenario(
    feature_file: str,
    machine_types: list[str],
    scenario_name: str = "",
    releases: list[str] | None = None,
    repo_root: str = "",
) -> str:
    try:
        resolved_repo_root = resolve_repo_root(repo_root or None)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    feature_validation_error = validate_feature_file(
        resolved_repo_root, feature_file
    )
    if feature_validation_error:
        return json.dumps({"ok": False, "error": feature_validation_error})

    if not machine_types:
        return json.dumps(
            {
                "ok": False,
                "error": "machine_types is required. Allowed values: lxd-container,lxd-vm",
            }
        )

    invalid_machine_types = sorted(
        machine_type
        for machine_type in machine_types
        if machine_type not in ALLOWED_MACHINE_TYPES
    )
    if invalid_machine_types:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "Unsupported machine_types: "
                    f"{','.join(invalid_machine_types)}. "
                    "Allowed values: lxd-container,lxd-vm"
                ),
            }
        )

    cloud_machine_types = sorted(
        machine_type
        for machine_type in machine_types
        if machine_type in CLOUD_MACHINE_TYPES
    )
    if cloud_machine_types and not _env_flag_enabled(
        ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR
    ):
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "Cloud machine_types are disabled by default. "
                    "Set "
                    f"{ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR}=1 "
                    "to allow: "
                    f"{','.join(cloud_machine_types)}"
                ),
            }
        )

    log_dir = resolve_log_dir(resolved_repo_root)
    job_id = uuid.uuid4().hex[:8]
    json_report_path = log_dir / f"{job_id}_report.json"
    stdout_path = log_dir / f"{job_id}_stdout.log"
    metadata_path = log_dir / f"{job_id}_meta.json"

    slot_error = _try_reserve_job_slot(job_id)
    if slot_error is not None:
        return json.dumps(slot_error)

    command = ["tox", "-e", "behave", "--", feature_file]
    if scenario_name:
        command.extend(["--name", scenario_name])
    if releases:
        command.extend(["-D", f"releases={','.join(releases)}"])
    command.extend(["-D", f"machine_types={','.join(machine_types)}"])
    command.extend(["-f", "json", "-o", str(json_report_path), "-f", "plain"])

    env = os.environ.copy()
    try:
        log_file = stdout_path.open("w", encoding="utf-8")
    except OSError as exc:
        _release_job_slot(job_id)
        return json.dumps(
            {
                "ok": False,
                "error": f"Failed to open log file for job_id {job_id}: {exc}",
            }
        )

    try:
        process = subprocess.Popen(
            command,
            cwd=str(resolved_repo_root),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception as exc:
        log_file.close()
        _release_job_slot(job_id)
        return json.dumps(
            {
                "ok": False,
                "error": f"Failed to start behave scenario: {exc}",
            }
        )

    with _JOBS_LOCK:
        ACTIVE_JOBS[job_id] = {
            "process": process,
            "json_report": json_report_path,
            "stdout_log": stdout_path,
            "metadata": metadata_path,
            "log_file_handle": log_file,
        }

    _write_job_metadata(
        metadata_path,
        {
            "job_id": job_id,
            "status": "started",
            "started_at": _utc_timestamp(),
            "feature_file": feature_file,
            "scenario_name": scenario_name,
            "machine_types": machine_types,
            "releases": releases or [],
            "command": command,
            "repo_root": str(resolved_repo_root),
            "artifacts": _job_artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_path,
                json_report=json_report_path,
                metadata=metadata_path,
            ),
        },
    )
    _append_job_index_event(
        log_dir,
        {
            "event": "started",
            "timestamp": _utc_timestamp(),
            "job_id": job_id,
            "feature_file": feature_file,
            "scenario_name": scenario_name,
            "machine_types": machine_types,
            "releases": releases or [],
            "artifacts": _job_artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_path,
                json_report=json_report_path,
                metadata=metadata_path,
            ),
        },
    )

    return json.dumps(
        {
            "ok": True,
            "job_id": job_id,
            "status": "started",
            "message": "Test started. Call wait_for_scenario_completion.",
            "artifacts": _job_artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_path,
                json_report=json_report_path,
                metadata=metadata_path,
            ),
        }
    )


@mcp.tool(
    description=(
        "Wait for a running behave job to complete by polling internally. "
        "Returns a compact structured summary on completion, or a timeout "
        "payload with recent output if the wait limit is reached."
    )
)
def wait_for_scenario_completion(
    job_id: str,
    max_wait_seconds: int = _DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    repo_root: str = "",
) -> str:
    if max_wait_seconds <= 0:
        return json.dumps(
            {
                "ok": False,
                "error": "max_wait_seconds must be greater than 0",
            }
        )
    if poll_interval_seconds <= 0:
        return json.dumps(
            {
                "ok": False,
                "error": "poll_interval_seconds must be greater than 0",
            }
        )

    deadline = time.monotonic() + max_wait_seconds

    while True:
        payload = _scenario_status_payload(job_id, repo_root or None)
        if payload.get("ok") is False:
            return json.dumps(payload)

        if payload.get("status") == "completed":
            return json.dumps(payload)

        if time.monotonic() >= deadline:
            return json.dumps(
                {
                    "ok": False,
                    "status": "timeout",
                    "job_id": job_id,
                    "max_wait_seconds": max_wait_seconds,
                    "poll_interval_seconds": poll_interval_seconds,
                    "last_status": "running",
                    "recent_output": payload.get("recent_output", ""),
                    "artifacts": payload.get("artifacts"),
                }
            )

        time.sleep(poll_interval_seconds)


def _scenario_status_payload(
    job_id: str, repo_root_override: str | None = None
) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)

    if job is None:
        try:
            job = _recover_job_from_disk(job_id, repo_root_override)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if job is None:
            return {"ok": False, "error": f"Unknown job_id: {job_id}"}

    process = job.get("process")
    stdout_log = Path(job["stdout_log"])
    json_report = Path(job["json_report"])
    metadata = Path(
        job.get("metadata") or stdout_log.with_name(f"{job_id}_meta.json")
    )
    log_dir = stdout_log.parent

    if process is not None:
        returncode = process.poll()
        if returncode is None:
            return {
                "ok": True,
                "status": "running",
                "job_id": job_id,
                "recent_output": _tail_file(
                    stdout_log, _DEFAULT_RUNNING_TAIL_LINES
                ),
                "artifacts": _job_artifacts_payload(
                    log_dir=log_dir,
                    stdout_log=stdout_log,
                    json_report=json_report,
                    metadata=metadata,
                ),
            }
    else:
        returncode = None

    log_file_handle = job.get("log_file_handle")
    if log_file_handle is not None and not log_file_handle.closed:
        log_file_handle.close()

    summary_payload = _summarize_behave_json(json_report)
    response: dict[str, Any] = {
        "ok": returncode == 0 if returncode is not None else False,
        "status": "completed",
        "job_id": job_id,
        "returncode": returncode,
        "artifacts": _job_artifacts_payload(
            log_dir=log_dir,
            stdout_log=stdout_log,
            json_report=json_report,
            metadata=metadata,
        ),
    }
    if summary_payload is None:
        response["summary"] = None
        response["failures"] = []
        response["recent_output"] = _tail_file(
            stdout_log, _DEFAULT_RUNNING_TAIL_LINES
        )
    else:
        response.update(summary_payload)

    existing_metadata = _read_metadata(metadata)
    existing_metadata.update(
        {
            "job_id": job_id,
            "status": "completed",
            "completed_at": _utc_timestamp(),
            "returncode": returncode,
            "ok": response["ok"],
            "artifacts": response["artifacts"],
        }
    )
    _write_job_metadata(metadata, existing_metadata)
    _append_job_index_event(
        log_dir,
        {
            "event": "completed",
            "timestamp": _utc_timestamp(),
            "job_id": job_id,
            "ok": response["ok"],
            "returncode": returncode,
            "artifacts": response["artifacts"],
        },
    )

    return response


@mcp.tool(
    description=(
        "Return a tail of the stdout log for a behave job. Use this for human "
        "debugging without flooding agent context with full logs."
    )
)
def get_scenario_logs(
    job_id: str,
    lines: int = _DEFAULT_LOG_TAIL_LINES,
    repo_root: str = "",
) -> str:
    lines = max(1, min(lines, _MAX_LOG_TAIL_LINES))

    with _JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)

    if job is None:
        try:
            job = _recover_job_from_disk(job_id, repo_root or None)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if job is None:
            return json.dumps(
                {"ok": False, "error": f"Unknown job_id: {job_id}"}
            )

    stdout_log = Path(job["stdout_log"])
    json_report = Path(job["json_report"])
    metadata = Path(
        job.get("metadata") or stdout_log.with_name(f"{job_id}_meta.json")
    )
    log_dir = stdout_log.parent
    if not stdout_log.exists():
        return json.dumps(
            {
                "ok": False,
                "error": f"No log file exists for job_id: {job_id}",
            }
        )

    return json.dumps(
        {
            "ok": True,
            "job_id": job_id,
            "lines": lines,
            "output": _tail_file(stdout_log, lines),
            "output_lines": _tail_lines(stdout_log, lines),
            "artifacts": _job_artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata,
            ),
        }
    )


@mcp.tool(
    description=(
        "Return artifact paths and metadata for a behave job so agents can "
        "parse full logs and reports from disk."
    )
)
def get_scenario_artifacts(job_id: str, repo_root: str = "") -> str:
    with _JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)

    if job is None:
        try:
            job = _recover_job_from_disk(job_id, repo_root or None)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if job is None:
            return json.dumps(
                {"ok": False, "error": f"Unknown job_id: {job_id}"}
            )

    stdout_log = Path(job["stdout_log"])
    json_report = Path(job["json_report"])
    metadata = Path(
        job.get("metadata") or stdout_log.with_name(f"{job_id}_meta.json")
    )
    log_dir = stdout_log.parent

    return json.dumps(
        {
            "ok": True,
            "job_id": job_id,
            "artifacts": _job_artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata,
            ),
            "metadata": _read_metadata(metadata),
            "exists": {
                "stdout_log": stdout_log.exists(),
                "json_report": json_report.exists(),
                "metadata": metadata.exists(),
            },
        }
    )


def resolve_repo_root(repo_root_override: str | None = None) -> Path:
    if repo_root_override:
        return _validated_repo_root(Path(repo_root_override).expanduser())

    env_value = os.environ.get("UBUNTU_PRO_CLIENT_REPO")
    if env_value:
        return _validated_repo_root(Path(env_value).expanduser())

    current = Path(__file__).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "features").exists() and (
            candidate / "tox.ini"
        ).exists():
            return candidate.resolve()

    return _validated_repo_root(current.parents[3])


def _validated_repo_root(candidate: Path) -> Path:
    resolved = candidate.resolve()
    features_dir = resolved / "features"
    tox_file = resolved / "tox.ini"
    if not features_dir.exists() or not tox_file.exists():
        raise ValueError(
            "Invalid repo_root: expected directory containing features/ and tox.ini"
        )
    return resolved


def validate_feature_file(repo_root: Path, feature_file: str) -> str | None:
    normalized = _normalize_feature_file_arg(feature_file)
    allowed_features = set(_discover_feature_files(repo_root))
    if normalized not in allowed_features:
        return "Feature is not listed by list_features: " f"{feature_file}"

    return None


def _discover_feature_files(repo_root: Path) -> list[str]:
    features_dir = repo_root / "features"
    if not features_dir.exists():
        return []

    return sorted(
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in features_dir.rglob("*.feature")
    )


def _normalize_feature_file_arg(feature_file: str) -> str:
    normalized = posixpath.normpath(feature_file.replace("\\", "/"))
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def resolve_log_dir(repo_root: Path) -> Path:
    env_path = os.environ.get("MCP_LOG_DIR")
    if env_path:
        log_dir = Path(env_path).resolve()
    else:
        log_dir = repo_root / ".mcp_behave_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _recover_job_from_disk(
    job_id: str, repo_root_override: str | None = None
) -> dict[str, Any] | None:
    repo_root = resolve_repo_root(repo_root_override)
    log_dir = resolve_log_dir(repo_root)
    stdout_log = log_dir / f"{job_id}_stdout.log"
    json_report = log_dir / f"{job_id}_report.json"
    metadata = log_dir / f"{job_id}_meta.json"
    if (
        not stdout_log.exists()
        and not json_report.exists()
        and not metadata.exists()
    ):
        return None

    metadata_payload = _read_metadata(metadata)
    if metadata_payload:
        metadata_artifacts = metadata_payload.get("artifacts", {})
        stdout_log = Path(
            metadata_artifacts.get("stdout_log", str(stdout_log))
        )
        json_report = Path(
            metadata_artifacts.get("json_report", str(json_report))
        )
        metadata = Path(metadata_artifacts.get("metadata", str(metadata)))

    return {
        "process": None,
        "json_report": json_report,
        "stdout_log": stdout_log,
        "metadata": metadata,
        "log_file_handle": None,
    }


def _try_reserve_job_slot(job_id: str) -> CapacityReservationError | None:
    max_parallel_jobs, parse_error = _configured_max_parallel_jobs()
    if parse_error:
        return {"ok": False, "error": parse_error}

    with _JOBS_LOCK:
        running_jobs = _count_running_or_reserved_jobs_locked()
        if running_jobs >= max_parallel_jobs:
            return {
                "ok": False,
                "status": "capacity_exceeded",
                "error": (
                    "Maximum parallel behave jobs reached. "
                    f"Set {MAX_PARALLEL_JOBS_ENV_VAR} to a higher value "
                    "or wait for an active job to complete."
                ),
                "capacity": {
                    "max_parallel_jobs": max_parallel_jobs,
                    "running_jobs": running_jobs,
                },
            }

        ACTIVE_JOBS[job_id] = {
            "slot_reserved": True,
            "reserved_at": _utc_timestamp(),
        }

    return None


def _release_job_slot(job_id: str) -> None:
    with _JOBS_LOCK:
        ACTIVE_JOBS.pop(job_id, None)


def _configured_max_parallel_jobs() -> tuple[int, str | None]:
    value = os.environ.get(MAX_PARALLEL_JOBS_ENV_VAR, "").strip()
    if not value:
        return _DEFAULT_MAX_PARALLEL_JOBS, None

    try:
        parsed_value = int(value)
    except ValueError:
        return (
            None,
            f"{MAX_PARALLEL_JOBS_ENV_VAR} must be a positive integer",
        )

    if parsed_value <= 0:
        return (
            None,
            f"{MAX_PARALLEL_JOBS_ENV_VAR} must be a positive integer",
        )

    return parsed_value, None


def _count_running_or_reserved_jobs_locked() -> int:
    running_jobs = 0
    for job in ACTIVE_JOBS.values():
        if job.get("slot_reserved"):
            running_jobs += 1
            continue

        process = job.get("process")
        if process is None:
            continue

        returncode = process.poll()
        if returncode is None:
            running_jobs += 1

    return running_jobs


def _job_artifacts_payload(
    *,
    log_dir: Path,
    stdout_log: Path,
    json_report: Path,
    metadata: Path,
) -> dict[str, str]:
    return {
        "log_dir": str(log_dir),
        "stdout_log": str(stdout_log),
        "json_report": str(json_report),
        "metadata": str(metadata),
    }


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_job_metadata(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Metadata write failures should not break job execution/status checks.
        return


def _append_job_index_event(
    log_dir: Path, event_payload: dict[str, Any]
) -> None:
    index_path = log_dir / _JOB_INDEX_FILE_NAME
    try:
        with index_path.open("a", encoding="utf-8") as index_stream:
            index_stream.write(
                json.dumps(event_payload, ensure_ascii=True, sort_keys=True)
                + "\n"
            )
    except OSError:
        # Index write failures should not break job execution/status checks.
        return


def _tail_file(path: Path, lines: int) -> str:
    if not path.exists():
        return "Waiting for output..."

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        tail = deque(stream, maxlen=lines)
    return "".join(tail).rstrip() if tail else "Waiting for output..."


def _tail_lines(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        tail = deque(stream, maxlen=lines)
    return [line.rstrip("\n") for line in tail]


def _scenario_status_from_steps(steps: list[dict[str, Any]]) -> str:
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


def _summarize_behave_json(
    report_path: Path,
) -> dict[str, Any] | None:
    if not report_path.exists():
        return None

    try:
        report_data = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(report_data, list):
        return None

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
    failures: list[dict[str, Any]] = []

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

            scenario_status = _scenario_status_from_steps(steps)
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
                        {
                            "feature": feature_name,
                            "scenario": scenario_name,
                            "step": step_name,
                            "status": step_status,
                            "error_message": error_message[:2000],
                        }
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

    return {"summary": summary, "failures": failures}


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)
