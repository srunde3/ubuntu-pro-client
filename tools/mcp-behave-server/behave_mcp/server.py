import json
import os
import time
import uuid
from datetime import datetime, timezone

from behave_mcp import domain
from behave_mcp.adapters import (
    EnvConfig,
    InMemoryJobRegistry,
    LocalArtifactStore,
    SubprocessProcessLauncher,
)
from behave_mcp.service import BehaveService
from mcp.server import FastMCP
from starlette.responses import JSONResponse

host = os.environ.get("MCP_HOST", "127.0.0.1")
port = int(os.environ.get("MCP_PORT", "8000"))
mcp = FastMCP("Ubuntu Pro Client Behave MCP", host=host, port=port)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


_config = EnvConfig()
_artifact_store = LocalArtifactStore()
registry = InMemoryJobRegistry()
_launcher = SubprocessProcessLauncher()
_service = BehaveService(
    config=_config,
    artifact_store=_artifact_store,
    registry=registry,
    launcher=_launcher,
    monotonic=time.monotonic,
    sleep=time.sleep,
    now_utc=_utc_timestamp,
    new_job_id=lambda: uuid.uuid4().hex[:8],
)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthcheck(request):
    return JSONResponse({"status": "ok"})


@mcp.tool(
    description=(
        "List feature files available in the repository so an agent can "
        "choose an allowed behave scenario."
    )
)
def list_features(repo_root: str = "") -> str:
    return json.dumps(_service.list_features(repo_root))


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
    return json.dumps(
        _service.start_scenario(
            feature_file,
            machine_types,
            scenario_name,
            releases,
            repo_root,
        )
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
    max_wait_seconds: int = domain._DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = domain._DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    repo_root: str = "",
) -> str:
    return json.dumps(
        _service.wait_for_completion(
            job_id,
            max_wait_seconds,
            poll_interval_seconds,
            repo_root,
        )
    )


@mcp.tool(
    description=(
        "Return a tail of the stdout log for a behave job. Use this for human "
        "debugging without flooding agent context with full logs."
    )
)
def get_scenario_logs(
    job_id: str,
    lines: int = domain._DEFAULT_LOG_TAIL_LINES,
    repo_root: str = "",
) -> str:
    return json.dumps(_service.get_logs(job_id, lines, repo_root))


@mcp.tool(
    description=(
        "Return artifact paths and metadata for a behave job so agents can "
        "parse full logs and reports from disk."
    )
)
def get_scenario_artifacts(job_id: str, repo_root: str = "") -> str:
    return json.dumps(_service.get_artifacts(job_id, repo_root))


def main() -> None:
    transport = _config.transport()
    mcp.run(transport=transport)
