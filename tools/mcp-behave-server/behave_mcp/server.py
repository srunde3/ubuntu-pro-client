import os
import time
import uuid
from datetime import datetime, timezone

from mcp.server import FastMCP
from starlette.responses import JSONResponse

from behave_mcp import domain
from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalArtifactStore,
    LocalFeatureCatalog,
    LocalFeatureFileReader,
    LocalWorkspace,
    PopenLauncher,
)
from behave_mcp.config import load_settings
from behave_mcp.service import BehaveService

host = os.environ.get("MCP_HOST", "127.0.0.1")
port = int(os.environ.get("MCP_PORT", "8000"))
mcp = FastMCP("Ubuntu Pro Client Behave MCP", host=host, port=port)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


_settings = load_settings(os.environ)
_workspace = LocalWorkspace()
_feature_reader = LocalFeatureFileReader()
_feature_catalog = LocalFeatureCatalog()
_artifact_store = LocalArtifactStore()
registry = InMemoryJobRegistry()
_launcher = PopenLauncher()
_service = BehaveService(
    workspace=_workspace,
    settings=_settings,
    feature_reader=_feature_reader,
    feature_catalog=_feature_catalog,
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
        "choose an allowed behave scenario. Returns a lightweight catalog "
        "entry per feature (path, title, scenario_count, requires_config, and "
        "the releases and machine_types it covers). Optional release, "
        "machine_type, tag, and text filters keep only features with at least "
        "one matching scenario. Use describe_feature for full per-scenario "
        "detail."
    )
)
def list_features(
    release: str = "",
    machine_type: str = "",
    tag: str = "",
    text: str = "",
    repo_root: str = "",
) -> str:
    return _service.list_features(
        release=release or None,
        machine_type=machine_type or None,
        tag=tag or None,
        text=text or None,
        repo_root=repo_root,
    ).model_dump_json()


@mcp.tool(
    description=(
        "Return full detail for a single feature file: its title, tags, "
        "required config, and every scenario with name, type, tags, required "
        "config, Examples column names, and the distinct (release, "
        "machine_type) combos it supports. feature_file must be a path "
        "returned by list_features."
    )
)
def describe_feature(feature_file: str, repo_root: str = "") -> str:
    return _service.describe_feature(feature_file, repo_root).model_dump_json()


@mcp.tool(
    description=(
        "List every distinct release and machine_type (substrate) used across "
        "the whole suite, each with a count of scenarios that reference it. "
        "Use this to discover valid values before filtering with "
        "list_features or find_scenarios."
    )
)
def list_dimensions(repo_root: str = "") -> str:
    return _service.list_dimensions(repo_root).model_dump_json()


@mcp.tool(
    description=(
        "Find scenarios across all features matching optional release, "
        "machine_type, tag, and text (scenario-name substring) filters. "
        "Returns matching feature_file, scenario_name, type, required config, "
        "and the combos that satisfy the release/machine_type filters."
    )
)
def find_scenarios(
    release: str = "",
    machine_type: str = "",
    tag: str = "",
    text: str = "",
    repo_root: str = "",
) -> str:
    return _service.find_scenarios(
        release=release or None,
        machine_type=machine_type or None,
        tag=tag or None,
        text=text or None,
        repo_root=repo_root,
    ).model_dump_json()


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
    return _service.start_scenario(
        feature_file,
        machine_types,
        scenario_name,
        releases,
        repo_root,
    ).model_dump_json()


@mcp.tool(
    description=(
        "List behave jobs. Returns every currently active job plus a "
        "bounded window of recently completed ones, merging in-memory state "
        "with jobs recovered from disk (e.g. after a server restart)."
    )
)
def list_scenario_jobs(repo_root: str = "") -> str:
    return _service.list_jobs(repo_root).model_dump_json()


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
    return _service.wait_for_completion(
        job_id,
        max_wait_seconds,
        poll_interval_seconds,
        repo_root,
    ).model_dump_json()


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
    return _service.get_logs(job_id, lines, repo_root).model_dump_json()


@mcp.tool(
    description=(
        "Return artifact paths and metadata for a behave job so agents can "
        "parse full logs and reports from disk."
    )
)
def get_scenario_artifacts(job_id: str, repo_root: str = "") -> str:
    return _service.get_artifacts(job_id, repo_root).model_dump_json()


def main() -> None:
    mcp.run(transport=_settings.transport)
