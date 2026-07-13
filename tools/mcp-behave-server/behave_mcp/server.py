import json
import os
import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

host = os.environ.get("MCP_HOST", "127.0.0.1")
port = int(os.environ.get("MCP_PORT", "8000"))
mcp = FastMCP("Ubuntu Pro Client Behave MCP", host=host, port=port)
ALLOWED_FEATURES = {"features/cli/attach.feature"}
ALLOWED_ENV_VARS = {
    "UACLIENT_BEHAVE_CONTRACT_TOKEN",
    "UACLIENT_BEHAVE_INSTALL_FROM",
}
ACTIVE_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_DEFAULT_RUNNING_TAIL_LINES = 12
_DEFAULT_LOG_TAIL_LINES = 200
_MAX_LOG_TAIL_LINES = 2000


@mcp.custom_route("/healthz", methods=["GET"])
async def healthcheck(request):
    return JSONResponse({"status": "ok"})


@mcp.tool(
    description=(
        "List feature files available in the repository so an agent can choose "
        "a supported behave scenario."
    )
)
def list_features() -> str:
    repo_root = resolve_repo_root()
    features_dir = repo_root / "features"
    if not features_dir.exists():
        return json.dumps({"features": []})

    feature_files = sorted(
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in features_dir.rglob("*.feature")
    )
    return json.dumps({"features": feature_files})


@mcp.tool(
    description=(
        "Start a whitelisted behave scenario through tox in the background and "
        "return a job_id immediately. Poll check_scenario_status for completion."
    )
)
def start_behave_scenario(
    feature_file: str,
    scenario_name: str = "",
    releases: list[str] | None = None,
    machine_types: list[str] | None = None,
) -> str:
    if feature_file not in ALLOWED_FEATURES:
        return json.dumps(
            {"ok": False, "error": f"Feature not allowed: {feature_file}"}
        )

    repo_root = resolve_repo_root()
    log_dir = resolve_log_dir(repo_root)
    job_id = uuid.uuid4().hex[:8]
    json_report_path = log_dir / f"{job_id}_report.json"
    stdout_path = log_dir / f"{job_id}_stdout.log"

    command = ["tox", "-e", "behave", "--", feature_file]
    if scenario_name:
        command.extend(["--name", scenario_name])
    if releases:
        command.extend(["-D", f"releases={','.join(releases)}"])
    if machine_types:
        command.extend(["-D", f"machine_types={','.join(machine_types)}"])
    command.extend(["-f", "json", "-o", str(json_report_path), "-f", "plain"])

    env = os.environ.copy()
    env.update(
        {
            key: os.environ[key]
            for key in sorted(ALLOWED_ENV_VARS)
            if key in os.environ
        }
    )
    log_file = stdout_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(repo_root),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )

    with _JOBS_LOCK:
        ACTIVE_JOBS[job_id] = {
            "process": process,
            "json_report": json_report_path,
            "stdout_log": stdout_path,
            "log_file_handle": log_file,
        }

    return json.dumps(
        {
            "ok": True,
            "job_id": job_id,
            "status": "started",
            "message": "Test started. Call check_scenario_status periodically.",
        }
    )


@mcp.tool(
    description=(
        "Check the status of a running behave job. Returns a log tail while "
        "running, and a compact structured summary when complete."
    )
)
def check_scenario_status(job_id: str) -> str:
    with _JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)

    if job is None:
        job = _recover_job_from_disk(job_id)
        if job is None:
            return json.dumps(
                {"ok": False, "error": f"Unknown job_id: {job_id}"}
            )

    process = job.get("process")
    stdout_log = Path(job["stdout_log"])
    json_report = Path(job["json_report"])

    if process is not None:
        returncode = process.poll()
        if returncode is None:
            return json.dumps(
                {
                    "ok": True,
                    "status": "running",
                    "job_id": job_id,
                    "recent_output": _tail_file(
                        stdout_log, _DEFAULT_RUNNING_TAIL_LINES
                    ),
                }
            )
    else:
        returncode = None

    log_file_handle = job.get("log_file_handle")
    if log_file_handle is not None and not log_file_handle.closed:
        log_file_handle.close()

    summary_payload = _summarize_behave_json(json_report)
    response: dict[str, Any] = {
        "status": "completed",
        "job_id": job_id,
        "returncode": returncode,
        "ok": returncode == 0 if returncode is not None else False,
    }
    if summary_payload is None:
        response["summary"] = None
        response["failures"] = []
        response["recent_output"] = _tail_file(
            stdout_log, _DEFAULT_RUNNING_TAIL_LINES
        )
    else:
        response.update(summary_payload)

    return json.dumps(response)


@mcp.tool(
    description=(
        "Return a tail of the stdout log for a behave job. Use this for human "
        "debugging without flooding agent context with full logs."
    )
)
def get_scenario_logs(
    job_id: str, lines: int = _DEFAULT_LOG_TAIL_LINES
) -> str:
    lines = max(1, min(lines, _MAX_LOG_TAIL_LINES))

    with _JOBS_LOCK:
        job = ACTIVE_JOBS.get(job_id)

    if job is None:
        job = _recover_job_from_disk(job_id)
        if job is None:
            return json.dumps(
                {"ok": False, "error": f"Unknown job_id: {job_id}"}
            )

    stdout_log = Path(job["stdout_log"])
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
        }
    )


def resolve_repo_root() -> Path:
    env_value = os.environ.get("UBUNTU_PRO_CLIENT_REPO")
    if env_value:
        return Path(env_value).resolve()

    current = Path(__file__).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "features").exists() and (
            candidate / "tox.ini"
        ).exists():
            return candidate

    return current.parents[3]


def resolve_log_dir(repo_root: Path) -> Path:
    env_path = os.environ.get("MCP_LOG_DIR")
    if env_path:
        log_dir = Path(env_path).resolve()
    else:
        log_dir = repo_root / ".mcp_behave_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _recover_job_from_disk(job_id: str) -> dict[str, Any] | None:
    repo_root = resolve_repo_root()
    log_dir = resolve_log_dir(repo_root)
    stdout_log = log_dir / f"{job_id}_stdout.log"
    json_report = log_dir / f"{job_id}_report.json"
    if not stdout_log.exists() and not json_report.exists():
        return None
    return {
        "process": None,
        "json_report": json_report,
        "stdout_log": stdout_log,
        "log_file_handle": None,
    }


def _tail_file(path: Path, lines: int) -> str:
    if not path.exists():
        return "Waiting for output..."

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        tail = deque(stream, maxlen=lines)
    return "".join(tail).rstrip() if tail else "Waiting for output..."


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
