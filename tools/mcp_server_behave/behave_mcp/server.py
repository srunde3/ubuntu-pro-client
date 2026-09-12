import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

from mcp.server import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from pydantic import Field
from starlette.responses import JSONResponse

from behave_campaign.adapters import (
    JsonlCampaignStore,
    ParserFeatureReader,
    system_now,
)
from behave_campaign.domain import Filters
from behave_campaign.messages import (
    DEFAULT_UNITS_LIMIT,
    CampaignStatusResponse,
    CreateCampaignResponse,
    ListCampaignsResponse,
)
from behave_campaign.repo import repo_state as read_repo_state
from behave_campaign.service import CampaignService
from behave_mcp import domain
from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalFeatureFileReader,
    LocalJobResultStoreFactory,
    LocalWorkspace,
    PopenLauncher,
)
from behave_mcp.config import ConfigError, Settings, load_settings
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
from behave_mcp.ports import JobRegistry
from behave_mcp.service import BehaveService

try:
    _settings = load_settings(os.environ)
except ConfigError as exc:
    print(f"mcp-server-behave: invalid configuration: {exc}", file=sys.stderr)
    raise SystemExit(1) from None

# mcp's Settings.lifespan field has an unresolved forward reference at class
# definition time; rebuilding it here, once, avoids an
# IncompleteFieldDefinitionWarning on FastMCP() construction below.
# Workaround for an upstream mcp bug, pinned to mcp==1.28.1 in pyproject.toml;
# safe to drop once upgrading mcp no longer triggers the warning.
FastMCPSettings.model_rebuild()

mcp = FastMCP(
    "Ubuntu Pro Client Behave MCP",
    host=_settings.host,
    port=_settings.port,
)

RepoRoot = Annotated[
    str,
    Field(
        default="",
        description=(
            "Repository to run behave against. Defaults to "
            "UBUNTU_PRO_CLIENT_REPO, then auto-detection -- which only "
            "works for editable/in-place installs (`uv run`), not `uvx`."
        ),
    ),
]
JobId = Annotated[
    str,
    Field(
        description=(
            "A job_id returned by start_scenario or list_scenario_jobs."
        )
    ),
]
ReleaseFilter = Annotated[
    str,
    Field(
        default="", description="Only keep scenarios covering this release."
    ),
]
MachineTypeFilter = Annotated[
    str,
    Field(
        default="",
        description="Only keep scenarios covering this machine_type.",
    ),
]
TagFilter = Annotated[
    str, Field(default="", description="Only keep scenarios with this tag.")
]
TextFilter = Annotated[
    str,
    Field(
        default="",
        description="Only keep scenarios whose name contains this substring.",
    ),
]
InstallFrom = Annotated[
    domain.InstallFrom,
    Field(
        default=domain.InstallFrom.LOCAL,
        description=(
            "Where to install ubuntu-pro-client from before running the "
            "scenario. Sets UACLIENT_BEHAVE_INSTALL_FROM for the behave "
            "subprocess. Defaults to 'local' (install from this repo "
            "checkout)."
        ),
    ),
]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_service(
    settings: Settings, *, registry: JobRegistry
) -> BehaveService:
    """Wire the production adapters into a ``BehaveService``."""
    return BehaveService(
        workspace=LocalWorkspace(),
        settings=settings,
        feature_reader=LocalFeatureFileReader(),
        results=LocalJobResultStoreFactory(),
        registry=registry,
        launcher=PopenLauncher(),
        monotonic=time.monotonic,
        sleep=time.sleep,
        now_utc=_utc_timestamp,
        new_job_id=lambda: uuid.uuid4().hex[:8],
    )


registry = InMemoryJobRegistry()
_service = create_service(_settings, registry=registry)
_workspace = LocalWorkspace()


def campaign_service(repo_root: str) -> CampaignService:
    """Build a CampaignService bound to this call's campaign directory.

    The directory follows repo_root, which varies per call, so the service
    is assembled per call the same way job result stores are bound per job.
    It holds no state of its own.
    """
    resolved = _workspace.resolve_repo_root(repo_root or None)
    return CampaignService(
        store=JsonlCampaignStore(_workspace.resolve_campaign_dir(resolved)),
        features=ParserFeatureReader(),
        now=system_now,
        repo_state=read_repo_state,
        max_parallel_jobs=_settings.max_parallel_jobs,
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
    release: ReleaseFilter = "",
    machine_type: MachineTypeFilter = "",
    tag: TagFilter = "",
    text: TextFilter = "",
    repo_root: RepoRoot = "",
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
    feature_file: Annotated[
        str, Field(description="A path returned by list_features.")
    ],
    repo_root: RepoRoot = "",
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
def list_dimensions(repo_root: RepoRoot = "") -> ListDimensionsResponse:
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
    release: ReleaseFilter = "",
    machine_type: MachineTypeFilter = "",
    tag: TagFilter = "",
    text: TextFilter = "",
    repo_root: RepoRoot = "",
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
        "rows. install_from controls where ubuntu-pro-client is installed "
        "from (defaults to 'local'; set to 'proposed' to test the "
        "-proposed pocket). Call wait_for_scenario_completion to wait for "
        "completion."
    )
)
def start_scenario(
    feature_file: Annotated[
        str, Field(description="A path returned by list_features.")
    ],
    machine_types: Annotated[
        list[str],
        Field(
            description=(
                "Required. Allowed values: lxd-container, lxd-vm. Cloud "
                "types (e.g. aws.generic) are blocked unless "
                "MCP_ALLOW_CLOUD_MACHINE_TYPES is set."
            )
        ),
    ],
    scenario_name: Annotated[
        str,
        Field(
            default="",
            description=(
                "Optional substring filter on scenario name, to run only "
                "matching Examples rows within feature_file."
            ),
        ),
    ] = "",
    releases: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Optional filter to only run Examples rows for these "
                "releases. Defaults to every release the scenario covers."
            ),
        ),
    ] = None,
    repo_root: RepoRoot = "",
    install_from: InstallFrom = domain.InstallFrom.LOCAL,
) -> StartScenarioResult:
    return _service.start_scenario(
        feature_file,
        machine_types,
        scenario_name,
        releases,
        repo_root,
        install_from,
    )


@mcp.tool(
    description=(
        "List behave jobs. Returns every currently active job plus a "
        "bounded window of recently completed ones (most recent first, "
        "capped at limit), merging in-memory state with jobs recovered "
        "from disk (e.g. after a server restart). total_completed is the "
        "full completed-job count regardless of limit, and truncated is "
        "set when older completed jobs were dropped to fit. limit must be "
        "positive (rejected otherwise); values above the server max are "
        "silently capped, with limit_clamped set to true when that "
        "happens."
    )
)
def list_scenario_jobs(
    repo_root: RepoRoot = "",
    limit: Annotated[
        int,
        Field(
            description=(
                "Max number of completed jobs to return (most recent "
                "first). Must be positive; values above the server max "
                "are silently capped."
            )
        ),
    ] = domain.DEFAULT_JOB_LIST_LIMIT,
) -> ListScenarioJobsResponse:
    return _service.list_jobs(repo_root, limit)


@mcp.tool(
    description=(
        "Summarize results across multiple behave jobs matching optional "
        "filters (job_ids, feature_file, scenario_name substring, release, "
        "machine_type, status). Returns job_counts (status totals -- how "
        "far into a set of runs you are), scenario-level pass/fail counts "
        "grouped by_release and by_machine_type (each job's scenarios are "
        "attributed to all of that job's declared releases/machine_types, "
        "not a specific Examples row), a flattened failures list tagged "
        "with job_id and release/machine_type context (capped at limit, "
        "with truncated set when more exist), and matched_job_ids for "
        "pivoting to get_scenario_logs/get_scenario_artifacts. limit must "
        "be positive (rejected otherwise); values above the server max "
        "are silently capped, with limit_clamped set to true when that "
        "happens. Provides raw status/data only -- rerunning failed "
        "scenarios and judging flaky-vs-real failures is left to the "
        "caller."
    )
)
def summarize_scenario_results(
    job_ids: Annotated[
        list[str] | None,
        Field(default=None, description="Only include these specific jobs."),
    ] = None,
    feature_file: Annotated[
        str,
        Field(default="", description="Only include jobs for this feature."),
    ] = "",
    scenario_name: Annotated[
        str,
        Field(
            default="",
            description=(
                "Only include jobs whose scenario_name contains this "
                "substring."
            ),
        ),
    ] = "",
    release: ReleaseFilter = "",
    machine_type: MachineTypeFilter = "",
    status: Annotated[
        str,
        Field(
            default="",
            description=(
                "Only include jobs with this status: running, "
                "completed, or unknown."
            ),
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(
            description=(
                "Max number of failures to return. Must be positive; "
                "values above the server max are silently capped."
            )
        ),
    ] = domain.DEFAULT_SUMMARIZE_FAILURES_LIMIT,
    repo_root: RepoRoot = "",
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
    job_id: JobId,
    max_wait_seconds: Annotated[
        int,
        Field(
            description="How long to poll before returning a timeout payload."
        ),
    ] = domain.DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[
        float, Field(description="Delay between internal status checks.")
    ] = domain.DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    repo_root: RepoRoot = "",
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
        "human debugging without flooding agent context with full logs. "
        "lines must be positive (rejected otherwise); values above the "
        "server max are silently capped, with lines_clamped set to true "
        "when that happens."
    )
)
def get_scenario_logs(
    job_id: JobId,
    lines: Annotated[
        int,
        Field(
            description=(
                "Number of trailing log lines to return. Must be "
                "positive; values above the server max are silently "
                "capped."
            )
        ),
    ] = domain.DEFAULT_LOG_TAIL_LINES,
    repo_root: RepoRoot = "",
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
    job_id: JobId, repo_root: RepoRoot = ""
) -> ArtifactsResponse:
    return _service.get_artifacts(job_id, repo_root)


@mcp.tool(
    description=(
        "Plan a test campaign and store it, without starting anything. A "
        "campaign is the durable record of which test units -- one scenario "
        "for one release on one machine_type -- are in scope, what has been "
        "attempted, and what each attempt established. Units are built only "
        "from the combinations each scenario actually supports, so the "
        "campaign is never a Cartesian product. Omit every filter to cover "
        "the whole suite. Use list_dimensions first to discover valid "
        "release and machine_type values. Returns the unit count so the "
        "scope can be confirmed before any test runs. max_lanes may not "
        "exceed MCP_MAX_PARALLEL_JOBS."
    )
)
def create_campaign(
    campaign_id: Annotated[
        str,
        Field(
            description=(
                "Identifier for this campaign, also its file name. For SRU "
                "work this is the Launchpad bug number. Letters, digits, "
                "'.', '_' and '-' only."
            )
        ),
    ],
    releases: Annotated[
        list[str],
        Field(description="Releases in scope."),
    ] = [],
    machine_types: Annotated[
        list[str],
        Field(description="Machine types in scope."),
    ] = [],
    features: Annotated[
        list[str],
        Field(
            description="Feature file paths in scope, from list_features.",
        ),
    ] = [],
    scenarios: Annotated[
        list[str],
        Field(
            description="Exact scenario names in scope.",
        ),
    ] = [],
    install_from: InstallFrom = domain.InstallFrom.LOCAL,
    max_lanes: Annotated[
        int,
        Field(
            default=1,
            description=(
                "How many behave jobs the campaign may run at once. Must "
                "not exceed MCP_MAX_PARALLEL_JOBS."
            ),
        ),
    ] = 1,
    repo_root: RepoRoot = "",
) -> CreateCampaignResponse:
    return campaign_service(repo_root).create_campaign(
        campaign_id=campaign_id,
        repo_root=_workspace.resolve_repo_root(repo_root or None),
        releases=releases,
        machine_types=machine_types,
        features=features,
        scenarios=scenarios,
        install_from=install_from.value,
        max_lanes=max_lanes,
    )


@mcp.tool(
    description=(
        "List every stored campaign with its current counts by unit state. "
        "Use it to find a campaign id to inspect, or to see what work is "
        "outstanding across campaigns."
    )
)
def list_campaigns(repo_root: RepoRoot = "") -> ListCampaignsResponse:
    return campaign_service(repo_root).list_campaigns()


@mcp.tool(
    description=(
        "Report one campaign's current state: counts by unit state, the "
        "units in flight, and the units needing action (failed, skipped or "
        "error). The full unit list is omitted by default because a full "
        "campaign is over a thousand units -- set include_units to get it, "
        "capped at limit with truncated saying whether any were dropped. "
        "The release, machine_type, feature, scenario and state filters "
        "narrow which units are counted and listed."
    )
)
def campaign_status(
    campaign_id: Annotated[
        str, Field(description="A campaign id from list_campaigns.")
    ],
    release: ReleaseFilter = "",
    machine_type: MachineTypeFilter = "",
    feature: Annotated[
        str, Field(default="", description="Only this feature file path.")
    ] = "",
    scenario: Annotated[
        str, Field(default="", description="Only this exact scenario name.")
    ] = "",
    state: Annotated[
        list[str],
        Field(
            description=(
                "Only units in these states: unattempted, running, passed, "
                "failed, skipped, error."
            ),
        ),
    ] = [],
    include_units: Annotated[
        bool,
        Field(default=False, description="Include the full unit list."),
    ] = False,
    limit: Annotated[
        int,
        Field(
            default=DEFAULT_UNITS_LIMIT,
            description="Cap on units returned when include_units is set.",
        ),
    ] = DEFAULT_UNITS_LIMIT,
    repo_root: RepoRoot = "",
) -> CampaignStatusResponse:
    return campaign_service(repo_root).campaign_status(
        campaign_id=campaign_id,
        filters=Filters(
            feature=(feature,) if feature else (),
            scenario=(scenario,) if scenario else (),
            release=(release,) if release else (),
            machine_type=(machine_type,) if machine_type else (),
            state=tuple(state),
        ),
        include_units=include_units,
        limit=limit,
    )


def main() -> None:
    mcp.run(transport=_settings.transport)
