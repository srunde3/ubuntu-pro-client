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
from behave_mcp.messages import (
    ArtifactsResponse,
    DescribeFeatureResponse,
    FindScenariosResponse,
    ListDimensionsResponse,
    ListFeaturesResponse,
    ListScenarioJobsResponse,
    LogsResponse,
    StartScenarioResult,
    SummarizeScenarioResultsResponse,
    WaitForCompletionResult,
)
from behave_mcp.service import BehaveService

_settings = load_settings(os.environ)
mcp = FastMCP(
    "Ubuntu Pro Client Behave MCP",
    host=_settings.host,
    port=_settings.port,
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


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
) -> ListFeaturesResponse:
    return _service.list_features(
        release=release or None,
        machine_type=machine_type or None,
        tag=tag or None,
        text=text or None,
        repo_root=repo_root,
    )


@mcp.tool(
    description=(
        "Return full detail for a single feature file: its title, tags, "
        "required config, and every scenario with name, type, tags, required "
        "config, Examples column names, and the distinct (release, "
        "machine_type) combos it supports. feature_file must be a path "
        "returned by list_features."
    )
)
def describe_feature(
    feature_file: str, repo_root: str = ""
) -> DescribeFeatureResponse:
    return _service.describe_feature(feature_file, repo_root)


@mcp.tool(
    description=(
        "List every distinct release and machine_type (substrate) used across "
        "the whole suite, each with a count of scenarios that reference it. "
        "Use this to discover valid values before filtering with "
        "list_features or find_scenarios."
    )
)
def list_dimensions(repo_root: str = "") -> ListDimensionsResponse:
    return _service.list_dimensions(repo_root)


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
) -> FindScenariosResponse:
    return _service.find_scenarios(
        release=release or None,
        machine_type=machine_type or None,
        tag=tag or None,
        text=text or None,
        repo_root=repo_root,
    )


@mcp.tool(
    description=(
        "Start a listed behave scenario through tox in the background and "
        "return a job_id immediately. feature_file must be a path returned "
        "by list_features. machine_types is required (lxd-container, "
        "lxd-vm; cloud machine types are blocked unless "
        "MCP_ALLOW_CLOUD_MACHINE_TYPES is set). scenario_name (substring) "
        "and releases are optional filters onto the feature's Examples "
        "rows. Call wait_for_scenario_completion to wait for completion."
    )
)
def start_scenario(
    feature_file: str,
    machine_types: list[str],
    scenario_name: str = "",
    releases: list[str] | None = None,
    repo_root: str = "",
) -> StartScenarioResult:
    return _service.start_scenario(
        feature_file,
        machine_types,
        scenario_name,
        releases,
        repo_root,
    )


@mcp.tool(
    description=(
        "List behave jobs. Returns every currently active job plus a "
        "bounded window of recently completed ones (most recent first, "
        "capped at limit), merging in-memory state with jobs recovered "
        "from disk (e.g. after a server restart). total_completed is the "
        "full completed-job count regardless of limit, and truncated is "
        "set when older completed jobs were dropped to fit."
    )
)
def list_scenario_jobs(
    repo_root: str = "",
    limit: int = domain.DEFAULT_JOB_LIST_LIMIT,
) -> ListScenarioJobsResponse:
    return _service.list_jobs(repo_root, limit)


@mcp.tool(
    description=(
        "Summarize results across multiple behave jobs matching optional "
        "filters (job_ids, feature_file, scenario_name substring, release, "
        "machine_type, status). Returns job_counts (status totals -- how "
        "far into a set of runs you are), scenario-level pass/fail counts "
        "grouped by_release and by_machine_type (each group flagged "
        "precise when every contributing scenario had a resolvable "
        "Examples-row location, false for jobs started before that "
        "tracking existed), a flattened failures list tagged with job_id "
        "and combo context (capped at limit, with truncated set when more "
        "exist), and matched_job_ids for pivoting to get_scenario_logs/"
        "get_scenario_artifacts. Provides raw status/data only -- rerunning "
        "failed scenarios and judging flaky-vs-real failures is left to "
        "the caller."
    )
)
def summarize_scenario_results(
    job_ids: list[str] | None = None,
    feature_file: str = "",
    scenario_name: str = "",
    release: str = "",
    machine_type: str = "",
    status: str = "",
    limit: int = domain.DEFAULT_SUMMARIZE_FAILURES_LIMIT,
    repo_root: str = "",
) -> SummarizeScenarioResultsResponse:
    return _service.summarize_scenario_results(
        job_ids=job_ids,
        feature_file=feature_file,
        scenario_name=scenario_name,
        release=release,
        machine_type=machine_type,
        status=status,
        limit=limit,
        repo_root=repo_root,
    )


@mcp.tool(
    description=(
        "Wait for a running behave job to complete by polling internally. "
        "job_id must come from start_scenario or list_scenario_jobs. "
        "Returns a compact structured summary on completion, or a timeout "
        "payload with recent output if the wait limit is reached."
    )
)
def wait_for_scenario_completion(
    job_id: str,
    max_wait_seconds: int = domain.DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = domain.DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    repo_root: str = "",
) -> WaitForCompletionResult:
    return _service.wait_for_completion(
        job_id,
        max_wait_seconds,
        poll_interval_seconds,
        repo_root,
    )


@mcp.tool(
    description=(
        "Return a tail of the stdout log for a behave job. job_id must "
        "come from start_scenario or list_scenario_jobs. Use this for "
        "human debugging without flooding agent context with full logs."
    )
)
def get_scenario_logs(
    job_id: str,
    lines: int = domain.DEFAULT_LOG_TAIL_LINES,
    repo_root: str = "",
) -> LogsResponse:
    return _service.get_logs(job_id, lines, repo_root)


@mcp.tool(
    description=(
        "Return artifact paths and metadata for a behave job so agents can "
        "parse full logs and reports from disk. job_id must come from "
        "start_scenario or list_scenario_jobs."
    )
)
def get_scenario_artifacts(
    job_id: str, repo_root: str = ""
) -> ArtifactsResponse:
    return _service.get_artifacts(job_id, repo_root)


def main() -> None:
    mcp.run(transport=_settings.transport)
