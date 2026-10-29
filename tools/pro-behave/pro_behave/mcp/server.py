import functools
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable

from mcp.server import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import TextContent
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from pro_behave.behave.framework import BehaveFramework
from pro_behave.campaign.adapters import (
    CatalogFeatureReader,
    JobDirFiles,
    JsonlCampaignStore,
    ServiceJobRunner,
    TarBundleWriter,
    system_now,
)
from pro_behave.campaign.domain.events import MAX_EVENT_ERROR_CHARS
from pro_behave.campaign.domain.records import Filters
from pro_behave.campaign.domain.schedule import ControlAction
from pro_behave.campaign.domain.state import GroupBy
from pro_behave.campaign.messages import (
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_PROBLEMS_LIMIT,
    AwaitEventsResponse,
    CampaignControlResponse,
    CampaignStatusResponse,
    CreateCampaignResponse,
    ExportCampaignResponse,
    ListCampaignsResponse,
    RetryUnitsResponse,
    UnitHistoryResponse,
)
from pro_behave.campaign.repo import repo_state as read_repo_state
from pro_behave.campaign.service import CampaignService
from pro_behave.jobs.adapters import (
    FileConfigStore,
    FileLanePool,
    FileReconcileLock,
    JobProcessLauncher,
    LocalJobResultStoreFactory,
    LocalWorkspace,
)
from pro_behave.jobs.domain import layout
from pro_behave.jobs.domain.jobs import InstallFrom
from pro_behave.jobs.domain.limits import (
    DEFAULT_FIND_LIMIT,
    DEFAULT_JOB_LIST_LIMIT,
    DEFAULT_LOG_CONTEXT,
    DEFAULT_LOG_LINES,
    DEFAULT_RESULTS_LIMIT,
    DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
)
from pro_behave.jobs.domain.pools import MAX_WSL_JOBS, Pool
from pro_behave.jobs.messages import (
    ArtifactsResponse,
    DescribeFeatureResponse,
    ErrorsResponse,
    FindScenariosResponse,
    JobStatusResult,
    KillJobResponse,
    ListDimensionsResponse,
    ListFeaturesResponse,
    ListJobsResponse,
    LogsResponse,
    ResultsResponse,
    RunJobResponse,
)
from pro_behave.jobs.service import (
    JobService,
    JobServiceError,
    UnknownJobError,
)
from pro_behave.mcp.settings import (
    ConfigError,
    ignored_behave_settings,
    load_settings,
)

logger = logging.getLogger(__name__)

# The test framework every job and campaign in this process uses.
FRAMEWORK = BehaveFramework()

# The module each job's process runs as (``pro_behave/job_process.py``).
JOB_PROCESS = "pro_behave.job_process"

try:
    _settings = load_settings(os.environ)
except ConfigError as exc:
    print(f"pro-behave mcp: invalid configuration: {exc}", file=sys.stderr)
    raise SystemExit(1) from None

# mcp's Settings.lifespan field has an unresolved forward reference at class
# definition time; rebuilding it here, once, avoids an
# IncompleteFieldDefinitionWarning on FastMCP() construction below.
# Workaround for an upstream mcp bug, pinned to mcp==1.28.1 in pyproject.toml;
# safe to drop once upgrading mcp no longer triggers the warning.
FastMCPSettings.model_rebuild()

mcp = FastMCP(
    "pro-behave",
    host=_settings.host,
    port=_settings.port,
)


def tool(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool that answers with one compact JSON text block.

    FastMCP would otherwise send every result twice -- pretty-printed text
    plus structuredContent -- and publish an outputSchema per tool that
    clients do not use. The compact form is a quarter smaller than the
    pretty one.

    Every call ends with a reconcile, whatever the tool, so queued jobs
    start as soon as anyone is talking to the server.
    """

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def as_text(*args: Any, **kw: Any) -> TextContent:
            try:
                return TextContent(
                    type="text", text=fn(*args, **kw).model_dump_json()
                )
            finally:
                _reconcile_quietly()

        return mcp.tool(structured_output=False, **kwargs)(as_text)

    return register


class CampaignMaxJobs(BaseModel):
    """Jobs at once per pool. A pool in scope left out gets 1."""

    lxd: int | None = Field(
        default=None, ge=1, description="lxd-container and lxd-vm."
    )
    cloud: int | None = Field(
        default=None, ge=1, description="aws.*, gcp.* and azure.*."
    )
    wsl: int | None = Field(
        default=None, ge=1, le=MAX_WSL_JOBS, description="wsl."
    )

    def requested(self) -> dict[Pool, int]:
        return {
            pool: count
            for pool, count in (
                (Pool.LXD, self.lxd),
                (Pool.CLOUD, self.cloud),
                (Pool.WSL, self.wsl),
            )
            if count is not None
        }


RepoRoot = Annotated[
    str,
    Field(
        default="",
        description="Checkout to use; defaults to UBUNTU_PRO_CLIENT_REPO.",
    ),
]
JobId = Annotated[
    str,
    Field(description=("A job_id returned by run_job or list_jobs.")),
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
InstallFromArg = Annotated[
    InstallFrom,
    Field(
        default=InstallFrom.LOCAL,
        description=(
            "Where to install ubuntu-pro-client from before running the "
            "scenario. Sets UACLIENT_BEHAVE_INSTALL_FROM for the behave "
            "subprocess. Defaults to 'local' (install from this repo "
            "checkout)."
        ),
    ),
]

CampaignIdArg = Annotated[
    str, Field(description="A campaign id from list_campaigns.")
]


# How often the server reconciles on its own, in case nothing else does.
RECONCILE_INTERVAL_SECONDS = 5.0


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_service() -> JobService:
    """The job service over the server's state directory, resolved per call:
    nothing about a job is held in this process."""
    workspace = LocalWorkspace()
    return JobService(
        workspace=workspace,
        config=FileConfigStore(workspace.resolve_state_dir),
        framework=FRAMEWORK,
        results=LocalJobResultStoreFactory(),
        lanes=FileLanePool(
            lambda: layout.lanes_dir(workspace.resolve_state_dir())
        ),
        launcher=JobProcessLauncher(workspace.resolve_jobs_dir, JOB_PROCESS),
        reconcile_lock=FileReconcileLock(workspace.resolve_state_dir),
        monotonic=time.monotonic,
        sleep=time.sleep,
        now_utc=_utc_timestamp,
        new_job_id=lambda: uuid.uuid4().hex[:8],
    )


def _reconcile_quietly() -> None:
    """Reconcile campaigns and the job queue, logging what goes wrong: a
    tool has its own answer to return, and the next call or the timer
    retries."""
    try:
        campaign_service().reconcile()
    except Exception:
        logger.exception("reconcile failed; the next caller retries it")


_service = create_service()
_workspace = LocalWorkspace()
_store: JsonlCampaignStore | None = None
_store_guard = threading.Lock()


def _campaign_dir() -> Path:
    return _workspace.resolve_campaign_dir()


def campaign_store() -> JsonlCampaignStore:
    """Return the one store for the server's campaign directory.

    Shared rather than per-call: a reader waiting for events is woken by
    the appends a reconcile in this process makes through this same object.
    """
    global _store
    campaign_dir = _campaign_dir()
    with _store_guard:
        if _store is None:
            _store = JsonlCampaignStore(campaign_dir)
        return _store


def campaign_service() -> CampaignService:
    """Build a CampaignService over the server's campaign store.

    It holds no state of its own, so it is assembled per call.
    """
    return CampaignService(
        store=campaign_store(),
        features=CatalogFeatureReader(FRAMEWORK.catalog),
        now=system_now,
        repo_state=read_repo_state,
        max_jobs_ceiling=_service.read_config().max_jobs,
        jobs=campaign_jobs(),
    )


def campaign_jobs() -> ServiceJobRunner:
    """The job service, as campaigns use it."""
    return ServiceJobRunner(
        _service, refusal=JobServiceError, unknown_job=UnknownJobError
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthcheck(request):
    return JSONResponse({"status": "ok"})


@tool(
    description=(
        "List feature files available in the repository so an agent can "
        "choose an allowed behave scenario. Returns a lightweight catalog "
        "entry per feature (path, title, scenario_count, requires_config, and "
        "the releases and machine_types it covers). Optional release, "
        "machine_type and tag filters keep only features with at least "
        "one matching scenario. Use describe_feature for full per-scenario "
        "detail."
    )
)
def list_features(
    release: ReleaseFilter = "",
    machine_type: MachineTypeFilter = "",
    tag: TagFilter = "",
    repo_root: RepoRoot = "",
) -> ListFeaturesResponse:
    return _service.list_features(
        release=release or None,
        machine_type=machine_type or None,
        tag=tag or None,
        repo_root=repo_root,
    )


@tool(
    description=(
        "Return full detail for a single feature file: its title, tags, "
        "required config, and every scenario with name, type, tags, required "
        "config, Examples column names, and the distinct (release, "
        "machine_type) combos it supports. feature must be a path "
        "returned by list_features."
    )
)
def describe_feature(
    feature: Annotated[
        str, Field(description="A path returned by list_features.")
    ],
    repo_root: RepoRoot = "",
) -> DescribeFeatureResponse:
    return _service.describe_feature(feature, repo_root)


@tool(
    description=(
        "List every distinct release and machine_type (substrate) used across "
        "the whole suite, each with a count of scenarios that reference it. "
        "Use this to discover valid values before filtering with "
        "list_features or find_scenarios."
    )
)
def list_dimensions(repo_root: RepoRoot = "") -> ListDimensionsResponse:
    return _service.list_dimensions(repo_root)


@tool(
    description=(
        "Find scenarios across all features matching optional release, "
        "machine_type and tag filters. "
        "Returns matching feature, scenario, type, required config, "
        "and the combos that satisfy the release/machine_type filters. "
        "limit caps the matches, with total and truncated alongside; "
        "narrow the filters rather than raising it."
    )
)
def find_scenarios(
    release: ReleaseFilter = "",
    machine_type: MachineTypeFilter = "",
    tag: TagFilter = "",
    limit: Annotated[
        int,
        Field(
            description=(
                "Most matches to return. Must be positive; values above "
                "the server max are capped, with limit_clamped set."
            )
        ),
    ] = DEFAULT_FIND_LIMIT,
    repo_root: RepoRoot = "",
) -> FindScenariosResponse:
    return _service.find_scenarios(
        release=release or None,
        machine_type=machine_type or None,
        tag=tag or None,
        repo_root=repo_root,
        limit=limit,
    )


@tool(
    description=(
        "Queue one unit -- one scenario on one release and one machine "
        "type -- to run through tox in the background, and return a job_id "
        "immediately. feature must be a path returned by "
        "list_features, and scenario a scenario's exact name from "
        "describe_feature that covers release and machine_type. A "
        "selection that matches no unit, or more than one, is refused. The "
        "machine type draws on the lxd, cloud or wsl pool: state is "
        "running when the pool had a free lane, else queued with "
        "queue_position (1 is next); a queued job starts by itself when a "
        "lane frees. A pool whose max jobs is 0 is disabled (cloud is by "
        "default). install_from controls where ubuntu-pro-client is "
        "installed from (defaults to 'local'; set to 'proposed' to test "
        "the -proposed pocket). Call wait_for_job to wait "
        "for it."
    )
)
def run_job(
    feature: Annotated[
        str, Field(description="A path returned by list_features.")
    ],
    scenario: Annotated[
        str,
        Field(description="A scenario's exact name, from describe_feature."),
    ],
    release: Annotated[
        str, Field(description="One release the scenario covers.")
    ],
    machine_type: Annotated[
        str,
        Field(
            description=(
                "One machine type the scenario covers. lxd-container and "
                "lxd-vm use the lxd pool, aws.*/gcp.*/azure.* the cloud "
                "pool, wsl the wsl pool."
            )
        ),
    ],
    repo_root: RepoRoot = "",
    install_from: InstallFromArg = InstallFrom.LOCAL,
) -> RunJobResponse:
    return _service.run_job(
        feature,
        scenario,
        release,
        machine_type,
        repo_root,
        install_from,
    )


@tool(
    description=(
        "List behave jobs. Returns every queued and running job plus a "
        "bounded window of recently ended ones (finished or interrupted; "
        "most recent first, capped at limit), read off disk, so jobs "
        "started by another process or before a restart are listed too. "
        "total_ended is the full ended-job count regardless of limit, and "
        "truncated is set when older ended jobs were dropped to fit. pools "
        "gives each pool's max jobs and busy lanes, so a queued job's wait "
        "is explained. "
        "limit must be positive (rejected otherwise); values above the "
        "server max are silently capped, with limit_clamped set to true "
        "when that happens."
    )
)
def list_jobs(
    limit: Annotated[
        int,
        Field(
            description=(
                "Max number of ended jobs to return (most recent "
                "first). Must be positive; values above the server max "
                "are silently capped."
            )
        ),
    ] = DEFAULT_JOB_LIST_LIMIT,
) -> ListJobsResponse:
    return _service.list_jobs(limit)


@tool(
    description=(
        "What came of each job: what it ran, its state, and once it "
        "finished its outcome (passed, failed, skipped, error) with the "
        "reason, each of the unit's instances, and every failing step. "
        "One result per job, most recently started first, the same shape "
        "wait_for_job returns for one job. Filter by "
        "job_ids, feature, scenario (exact name), release, "
        "machine_type and state (queued, running, finished, interrupted); "
        "limit "
        "caps the jobs listed, with total and truncated alongside. For "
        "the cause behind an empty error_message, job_errors."
    )
)
def job_results(
    job_ids: Annotated[
        list[str] | None,
        Field(default=None, description="Only these jobs."),
    ] = None,
    feature: Annotated[
        str,
        Field(default="", description="Only jobs for this feature."),
    ] = "",
    scenario: Annotated[
        str,
        Field(
            default="",
            description="Only jobs for the scenario of this exact name.",
        ),
    ] = "",
    release: ReleaseFilter = "",
    machine_type: MachineTypeFilter = "",
    state: Annotated[
        str,
        Field(
            default="",
            description="Only jobs in this state: queued, running, "
            "finished or interrupted.",
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(
            description=(
                "Most jobs to return. Must be positive; values above the "
                "server max are capped, with limit_clamped set."
            )
        ),
    ] = DEFAULT_RESULTS_LIMIT,
) -> ResultsResponse:
    return _service.job_results(
        job_ids=job_ids,
        feature=feature,
        scenario=scenario,
        release=release,
        machine_type=machine_type,
        state=state,
        limit=limit,
    )


@tool(
    description=(
        "Wait for a queued or running behave job to end by polling "
        "internally. job_id must come from run_job or "
        "list_jobs. Returns state finished with a compact "
        "result (outcome, reason, instances, failures); state interrupted "
        "with recent output when it ended without a result, with "
        "interruption killed or died (its process "
        "died: a crash or a reboot); or, if the wait runs out first, the "
        "job as it stands (queued or running) with timed_out true."
    )
)
def wait_for_job(
    job_id: JobId,
    max_wait_seconds: Annotated[
        int,
        Field(description="How long to poll before returning with timed_out."),
    ] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: Annotated[
        float, Field(description="Delay between internal status checks.")
    ] = DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
) -> JobStatusResult:
    return _service.wait_for_job(
        job_id,
        max_wait_seconds,
        poll_interval_seconds,
    )


@tool(
    description=(
        "What went wrong in a behave job, from its log: every traceback, "
        "hook error and failed assertion in the order they happened, each "
        "with its line range, the failing step above it, and the "
        "exception raised. Use this first on a failed job -- the first "
        "region is usually the cause and later ones its consequences (a "
        "cleanup hook failing because the machine never existed). Then "
        "job_logs(start=first_line) to read around one. When "
        "errors is empty but the job failed, behave itself may not have "
        "run: read tail, or job_logs with a pattern."
    )
)
def job_errors(job_id: JobId) -> ErrorsResponse:
    return _service.job_errors(job_id)


@tool(
    description=(
        "Read part of a behave job's stdout log; every returned line is "
        "prefixed 'N: ' with its 1-based line number. With pattern (a "
        "regex, case-insensitive): the matching lines from start on, each "
        "with context lines around it, grep-style, until the next window "
        "would exceed lines. Without: lines lines from start, or the last "
        "lines lines when start is 0. matches counts every match whether "
        "or not it fit; truncated says the budget cut something. Use it "
        "to locate (pattern='Traceback|Error', context=0) and then read "
        "around (start=N)."
    )
)
def job_logs(
    job_id: JobId,
    pattern: Annotated[
        str,
        Field(
            default="",
            description="Regex to search for; empty reads a range instead.",
        ),
    ] = "",
    context: Annotated[
        int,
        Field(
            default=DEFAULT_LOG_CONTEXT,
            description="Lines to include either side of each match.",
        ),
    ] = DEFAULT_LOG_CONTEXT,
    start: Annotated[
        int,
        Field(
            default=0,
            description=(
                "1-based line to read or search from. 0 means the end of "
                "the log when reading, the beginning when searching."
            ),
        ),
    ] = 0,
    lines: Annotated[
        int,
        Field(
            description=(
                "Most lines to return. Must be positive; values above "
                "the server max are capped, with lines_clamped set."
            )
        ),
    ] = DEFAULT_LOG_LINES,
) -> LogsResponse:
    return _service.job_logs(
        job_id,
        lines,
        pattern=pattern,
        context=context,
        start=start,
    )


@tool(
    description=(
        "Return artifact paths and metadata for a behave job so agents can "
        "parse full logs and reports from disk. job_id must come from "
        "run_job or list_jobs."
    )
)
def job_artifacts(job_id: JobId) -> ArtifactsResponse:
    return _service.job_artifacts(job_id)


@tool(
    description=(
        "Plan a test campaign and store it, without starting anything. A "
        "campaign is the durable record of which test units -- one scenario "
        "for one release on one machine_type -- are in scope, what has been "
        "attempted, and what each attempt established. Units are built only "
        "from the combinations each scenario actually supports, so the "
        "campaign is never a Cartesian product. Omit every filter to cover "
        "the whole suite. Use list_dimensions first to discover valid "
        "release and machine_type values. Returns the unit count so the "
        "scope can be confirmed before any test runs. max_jobs sets how "
        "many of its jobs may be in flight at once in each pool the units "
        "draw on (lxd, cloud, wsl); a pool left out gets 1, and none may "
        "exceed the checkout's max jobs for that pool."
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
    install_from: InstallFromArg = InstallFrom.LOCAL,
    max_jobs: CampaignMaxJobs = CampaignMaxJobs(),
    repo_root: RepoRoot = "",
) -> CreateCampaignResponse:
    return campaign_service().create_campaign(
        campaign_id=campaign_id,
        repo_root=_workspace.resolve_repo_root(repo_root or None),
        releases=releases,
        machine_types=machine_types,
        features=features,
        scenarios=scenarios,
        install_from=install_from.value,
        max_jobs=max_jobs.requested(),
    )


@tool(
    description=(
        "List every stored campaign with its current counts by unit state. "
        "Use it to find a campaign id to inspect, or to see what work is "
        "outstanding across campaigns."
    )
)
def list_campaigns() -> ListCampaignsResponse:
    return campaign_service().list_campaigns()


@tool(
    description=(
        "Report one campaign's current state: counts by unit state, jobs "
        "in flight in each pool, the units in flight, and the units needing "
        "action (failed, skipped or error) -- one unit per row, or with "
        "group_by='scenario' one "
        "scenario per row with its units bucketed by state, which is how "
        "to see a scenario failing on every release at a glance. The "
        "release, machine_type, feature, scenario and state filters narrow "
        "which units are counted and listed; unit_history lists individual "
        "units."
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
                "Only units in these states: unattempted, in_flight, passed, "
                "failed, skipped, error."
            ),
        ),
    ] = [],
    group_by: Annotated[
        GroupBy,
        Field(
            default=GroupBy.UNIT,
            description=(
                "How problems are listed: 'unit' (one per row) or "
                "'scenario' (one scenario per row, units by state)."
            ),
        ),
    ] = GroupBy.UNIT,
    problems_limit: Annotated[
        int,
        Field(
            default=DEFAULT_PROBLEMS_LIMIT,
            description=(
                "Most problem rows to list; problems_total counts them all."
            ),
        ),
    ] = DEFAULT_PROBLEMS_LIMIT,
) -> CampaignStatusResponse:
    return campaign_service().campaign_status(
        campaign_id=campaign_id,
        filters=Filters(
            feature=(feature,) if feature else (),
            scenario=(scenario,) if scenario else (),
            release=(release,) if release else (),
            machine_type=(machine_type,) if machine_type else (),
            state=tuple(state),
        ),
        group_by=group_by.value,
        problems_limit=problems_limit,
    )


@tool(
    description=(
        "Every attempt at each selected unit, oldest first: the job it "
        "ran as, what it installed from, when, and its outcome. This is "
        "how to see that a unit errored, was retried and then passed "
        "without reading the event stream. Narrow with the release, "
        "machine_type, feature, scenario and state filters; a single "
        "scenario across its releases is the usual question. limit caps "
        "the units listed, with truncated saying whether any were dropped."
    )
)
def unit_history(
    campaign_id: CampaignIdArg,
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
                "Only units in these states: unattempted, in_flight, passed, "
                "failed, skipped, error."
            ),
        ),
    ] = [],
    limit: Annotated[
        int,
        Field(
            default=DEFAULT_HISTORY_LIMIT,
            description="Most units to list.",
        ),
    ] = DEFAULT_HISTORY_LIMIT,
) -> UnitHistoryResponse:
    return campaign_service().unit_history(
        campaign_id=campaign_id,
        filters=Filters(
            feature=(feature,) if feature else (),
            scenario=(scenario,) if scenario else (),
            release=(release,) if release else (),
            machine_type=(machine_type,) if machine_type else (),
            state=tuple(state),
        ),
        limit=limit,
    )


@tool(
    description=(
        "Run or pause a campaign. A campaign's state is its intent (what "
        "was last asked: never_run, run or hold) plus its progress (how "
        "much work is left). Any number of campaigns may run at once.\n"
        "  run    intent -> run, from any state. Queues up to the "
        "campaign's max_jobs at once; the server records each outcome and "
        "queues the next unit on its own, with no further calls: running "
        "while units are left to queue, finishing while the last jobs run, "
        "then complete. Running a complete campaign changes nothing; use "
        "retry_units to give it work.\n"
        "  pause  intent -> hold, from any state. No new job starts; jobs "
        "in flight finish and are recorded (pausing), then the campaign is "
        "paused, or complete if nothing is left. With kill=true the jobs "
        "in flight are killed instead: their units are recorded as "
        "interrupted and run again on the next run; killed lists them, "
        "and not_killed names any job whose process could not be "
        "reached.\n"
        "Returns state, intent, progress, in_flight and counts. Follow "
        "progress with await_campaign_events or campaign_status."
    )
)
def control_campaign(
    campaign_id: CampaignIdArg,
    action: Annotated[
        ControlAction,
        Field(description="run or pause; see above."),
    ],
    kill: Annotated[
        bool,
        Field(
            default=False,
            description="With pause: kill the jobs in flight.",
        ),
    ] = False,
) -> CampaignControlResponse:
    campaigns = campaign_service()
    if action == ControlAction.RUN:
        if kill:
            raise ValueError("kill applies only to pause")
        return campaigns.run(campaign_id=campaign_id)
    return campaigns.pause(campaign_id=campaign_id, kill=kill)


@tool(
    description=(
        "Wait for news about a campaign. Returns every event after "
        "since_seq, blocking up to timeout_seconds for something to happen, "
        "and always reports the campaign's state -- so a "
        "batch that comes back empty still says where things stand. Pass "
        "next_seq from the previous response as the next since_seq to read "
        "the stream without gaps or repeats. kinds defaults to the "
        "actionable preset; pass ['*'] for everything, or exact kinds and "
        "families (campaign.*, job.*, unit.*, anomaly.*). A unit.failed "
        "event carries the failing steps, so a failure can be judged "
        "without fetching the job's report."
    )
)
def await_campaign_events(
    campaign_id: CampaignIdArg,
    since_seq: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Return events numbered above this. Use next_seq from the "
                "previous response; 0 starts from the beginning."
            ),
        ),
    ] = 0,
    kinds: Annotated[
        list[str],
        Field(
            description=(
                "Event kinds (unit.failed), families (unit.*), or the "
                "presets 'actionable' (every non-passing outcome, anomaly.*, "
                "job.overdue, campaign.*) and '*' (everything). Defaults to "
                "actionable; progress is in counts on every response."
            ),
        ),
    ] = ["actionable"],
    timeout_seconds: Annotated[
        int,
        Field(
            default=0,
            description=(
                "How long to wait for a matching event. 0 returns whatever "
                "is already there without blocking. Above the server's "
                "PRO_BEHAVE_MCP_POLL_TIMEOUT the call is rejected rather "
                "than quietly shortened."
            ),
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            default=DEFAULT_EVENTS_LIMIT,
            description="Most events to return in one batch.",
        ),
    ] = DEFAULT_EVENTS_LIMIT,
    failure_chars: Annotated[
        int,
        Field(
            default=MAX_EVENT_ERROR_CHARS,
            description=(
                "Cut each failure's error_message to this many characters; "
                "0 drops the messages, keeping step and status."
            ),
        ),
    ] = MAX_EVENT_ERROR_CHARS,
) -> AwaitEventsResponse:
    if timeout_seconds > _settings.campaign_poll_timeout:
        raise ValueError(
            "timeout_seconds {} exceeds this server's limit of {}; a longer "
            "wait would outlast the client's own request timeout".format(
                timeout_seconds, _settings.campaign_poll_timeout
            )
        )
    return campaign_service().await_events(
        campaign_id=campaign_id,
        since_seq=since_seq,
        kinds=kinds,
        limit=limit,
        timeout=float(max(timeout_seconds, 0)),
        failure_chars=failure_chars,
    )


@tool(
    description=(
        "Ask for another attempt at units that already had one. Nothing "
        "re-runs a non-passing unit on its own, so this is the only way a "
        "failure gets retried. With no state filter it selects the problem "
        "states -- failed, skipped and errored; name a state to retry "
        "something else, including one that passed. Units in flight are "
        "never selected. A retry always sets the campaign's intent to run, "
        "whatever its state, so the selected units start at once."
    )
)
def retry_units(
    campaign_id: CampaignIdArg,
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
                "Only units in these states. Defaults to failed, skipped "
                "and errored."
            ),
        ),
    ] = [],
    reason: Annotated[
        str,
        Field(
            default="",
            description=(
                "Why these are being re-run, recorded with the request."
            ),
        ),
    ] = "",
) -> RetryUnitsResponse:
    return campaign_service().retry_units(
        campaign_id=campaign_id,
        filters=Filters(
            feature=(feature,) if feature else (),
            scenario=(scenario,) if scenario else (),
            release=(release,) if release else (),
            machine_type=(machine_type,) if machine_type else (),
            state=tuple(state),
        ),
        reason=reason,
    )


@tool(
    description=(
        "Write a complete campaign's evidence bundle, a .tar.gz for an SRU "
        "bug: SUMMARY.md and units.csv for a reviewer, export.json, every "
        "attempt's job files, and the raw campaign files. Only a complete "
        "campaign exports. Written under the state directory's exports/, "
        "never over an existing file. Returns the path, the counts, how "
        "many jobs were bundled and any job files that were missing."
    )
)
def export_campaign(campaign_id: CampaignIdArg) -> ExportCampaignResponse:
    state = _workspace.resolve_state_dir()
    return campaign_service().export_campaign(
        campaign_id=campaign_id,
        output=layout.export_file(state, campaign_id),
        jobs=JobDirFiles(layout.jobs_dir(state)),
        writer=TarBundleWriter(),
    )


@tool(
    description=(
        "Kill a queued or running behave job, whichever process started "
        "it. A queued job never starts. Use it for a running job that has "
        "hung: tox is stopped and allowed to clean up, "
        "and the job ends interrupted with interruption killed, never as a "
        "failure; a running campaign records its attempt as interrupted "
        "and queues the unit again. killed is false when there was nothing "
        "to stop."
    )
)
def kill_job(job_id: JobId) -> KillJobResponse:
    return _service.kill_job(job_id)


def _reconcile_forever() -> None:
    while True:
        time.sleep(RECONCILE_INTERVAL_SECONDS)
        _reconcile_quietly()


def main() -> None:
    ignored = ignored_behave_settings(os.environ)
    if ignored:
        print(
            "pro-behave mcp: ignoring {}: jobs take behave settings only "
            "from the checkout's config. Set each with pro-behave "
            "config set behave.<option> VALUE.".format(", ".join(ignored)),
            file=sys.stderr,
        )
    threading.Thread(
        target=_reconcile_forever, name="reconcile", daemon=True
    ).start()
    mcp.run(transport="streamable-http")
