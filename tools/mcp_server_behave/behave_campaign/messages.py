"""Typed request/response DTOs for the campaign MCP tools."""

from pydantic import BaseModel

# A full campaign is over a thousand units, so status reports counts plus
# the units that need attention. The whole list is opt-in and capped.
DEFAULT_UNITS_LIMIT = 200
MAX_UNITS_LIMIT = 2000
DEFAULT_EVENTS_LIMIT = 100
MAX_EVENTS_LIMIT = 1000


class UnitView(BaseModel):
    """One planned unit and its current state."""

    feature: str = ""
    scenario: str = ""
    release: str = ""
    machine_type: str = ""
    state: str = ""
    job_id: str | None = None
    attempt_count: int = 0


class ProblemUnit(BaseModel):
    """A problem unit under its scenario: the combo, and where to look."""

    release: str = ""
    machine_type: str = ""
    job_id: str | None = None
    attempt_count: int = 0


class ScenarioProblems(BaseModel):
    """One scenario's problem units, bucketed by state.

    Units are ``{release, machine_type}`` combos, the catalog's own object,
    so "failed on every release" reads off one row.
    """

    feature: str = ""
    scenario: str = ""
    failed: list[ProblemUnit] = []
    skipped: list[ProblemUnit] = []
    error: list[ProblemUnit] = []


class StateCounts(BaseModel):
    """How many units sit in each state."""

    unattempted: int = 0
    running: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    error: int = 0


class CampaignRepo(BaseModel):
    """Checkout a campaign was built from.

    ``commit``/``branch`` are None when the root isn't a git checkout;
    ``dirty`` is None whenever ``commit`` is.
    """

    root: str = ""
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None


class CampaignScope(BaseModel):
    """Filters the campaign was created from. Empty means unrestricted."""

    feature: list[str] = []
    scenario: list[str] = []
    release: list[str] = []
    machine_type: list[str] = []


class CampaignHeader(BaseModel):
    """How a campaign was built. Fixed at creation."""

    campaign_id: str = ""
    created_at: str = ""
    install_from: str = ""
    max_lanes: int = 0
    total_units: int = 0
    repo: CampaignRepo = CampaignRepo()
    scope: CampaignScope = CampaignScope()


class CampaignState(BaseModel):
    """Where a campaign stands now.

    Every response that changes or follows a campaign carries this and no
    more; the header is one ``campaign_status`` away.
    """

    campaign_id: str = ""
    lifecycle: str = ""
    lanes_busy: int = 0
    counts: StateCounts = StateCounts()


class ScopeSize(BaseModel):
    """How many values each scope filter names. Zero means unrestricted."""

    features: int = 0
    scenarios: int = 0
    releases: int = 0
    machine_types: int = 0


class CampaignListing(CampaignState):
    """One row of ``list_campaigns``: enough to tell campaigns apart."""

    created_at: str = ""
    install_from: str = ""
    max_lanes: int = 0
    total_units: int = 0
    scope_size: ScopeSize = ScopeSize()


class CreateCampaignResponse(BaseModel):
    """Result of creating a campaign. Nothing is running yet."""

    campaign: CampaignHeader = CampaignHeader()
    state: CampaignState = CampaignState()


class ListCampaignsResponse(BaseModel):
    """Every stored campaign, oldest id first."""

    campaign_dir: str = ""
    campaigns: list[CampaignListing] = []


class RecordAttemptsResponse(CampaignState):
    """Result of appending attempts: how many landed, and the state after."""

    recorded: int = 0
    running: list[UnitView] = []
    problems: list[UnitView] = []


class CampaignStatusResponse(BaseModel):
    """Current state of one campaign.

    ``running`` and the problems are always present because they are what
    a caller acts on: ``problems`` lists them one unit per row, or
    ``problem_scenarios`` one scenario per row when grouped that way;
    the other is None. ``units`` is None unless a ``units_limit`` was
    asked for, and is capped at it, with ``truncated`` saying whether any
    were dropped.
    """

    campaign: CampaignHeader = CampaignHeader()
    state: CampaignState = CampaignState()
    running: list[UnitView] = []
    problems: list[UnitView] | None = None
    problem_scenarios: list[ScenarioProblems] | None = None
    units: list[UnitView] | None = None
    truncated: bool = False
    limit_clamped: bool = False


class DimensionValue(BaseModel):
    """A release or machine_type, with how many scenarios reference it."""

    name: str = ""
    scenario_count: int = 0


class DimensionsResponse(BaseModel):
    """Releases and machine types the feature files can run."""

    releases: list[DimensionValue] = []
    machine_types: list[DimensionValue] = []


class AttemptView(BaseModel):
    """One try at a unit: one job, from start to finish.

    ``outcome`` is None while the job is still running.
    """

    job_id: str = ""
    install_from: str = ""
    started_at: str = ""
    outcome: str | None = None
    finished_at: str = ""


class UnitHistoryView(UnitView):
    """A unit with every attempt against it, oldest first."""

    attempts: list[AttemptView] = []


class NextUnitsResponse(BaseModel):
    """Units to run next: never-attempted first, then retryable problems."""

    units: list[UnitView] = []


class UnitHistoryResponse(BaseModel):
    """Every selected unit with its full attempt history."""

    units: list[UnitHistoryView] = []
    truncated: bool = False
    limit_clamped: bool = False


class CampaignControlResponse(CampaignState):
    """State of a campaign after a control verb.

    ``lanes_busy`` is what tells a caller whether a pause or cancel has
    finished draining: both stop opening lanes, but jobs already in flight
    run to completion and are still recorded.
    """

    reason: str = ""


class ReopenCampaignResponse(CampaignControlResponse):
    """State of a campaign after a cancellation was reversed.

    ``abandoned`` lists units whose jobs nothing was watching any more:
    reopening records them as errored, so they are retryable rather than
    running for good. ``rescheduling`` says whether lanes start filling
    again -- a campaign whose units were all attempted comes back complete,
    and needs retry_units to give it work.
    """

    abandoned: list[UnitView] = []
    rescheduling: bool = False


class TickReport(BaseModel):
    """What one scheduler tick did.

    ``problems`` carries anything that needed saying: a result that could
    not be classified, or a lane that would not open.
    """

    campaign_id: str = ""
    lifecycle: str = ""
    recorded: int = 0
    started: int = 0
    lanes_busy: int = 0
    problems: list[str] = []
    finished: bool = False


class EventView(BaseModel):
    """One numbered event. ``data`` is kind-specific."""

    seq: int = 0
    kind: str = ""
    at: str = ""
    data: dict = {}


class AwaitEventsResponse(CampaignState):
    """A batch of events, plus where the campaign stands.

    ``next_seq`` is the cursor to pass back. The state is always present,
    so a batch that came back empty on timeout still says what is
    happening -- which is why there is no separate heartbeat event.
    """

    events: list[EventView] = []
    next_seq: int = 0
    latest_seq: int = 0
    timed_out: bool = False


class RetryUnitsResponse(CampaignState):
    """Which units were re-queued, and what the campaign is doing now.

    ``rescheduling`` says whether lanes will actually start filling: a
    paused campaign accepts retries but stays paused until it is resumed.
    """

    requeued: int = 0
    units: list[UnitView] = []
    rescheduling: bool = False
