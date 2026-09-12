"""Typed request/response DTOs for the campaign MCP tools."""

from pydantic import BaseModel

# A full campaign is over a thousand units, so status reports counts plus
# the units that need attention. The whole list is opt-in and capped.
DEFAULT_UNITS_LIMIT = 200
MAX_UNITS_LIMIT = 2000


class UnitView(BaseModel):
    """One planned unit and its current state."""

    feature: str = ""
    scenario: str = ""
    release: str = ""
    machine_type: str = ""
    state: str = ""
    job_id: str | None = None
    attempt_count: int = 0


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


class CampaignSummary(BaseModel):
    """A campaign's header plus its current counts."""

    campaign_id: str = ""
    created_at: str = ""
    install_from: str | None = None
    max_lanes: int | None = None
    total_units: int = 0
    counts: StateCounts = StateCounts()
    repo: CampaignRepo = CampaignRepo()
    scope: CampaignScope = CampaignScope()


class CreateCampaignResponse(BaseModel):
    """Result of creating a campaign. Nothing is running yet."""

    campaign: CampaignSummary = CampaignSummary()


class ListCampaignsResponse(BaseModel):
    """Every stored campaign, oldest id first."""

    campaign_dir: str = ""
    campaigns: list[CampaignSummary] = []


class RecordAttemptsResponse(BaseModel):
    """Result of appending attempts: how many landed, and the state after."""

    recorded: int = 0
    campaign: CampaignSummary = CampaignSummary()
    running: list[UnitView] = []
    problems: list[UnitView] = []


class CampaignStatusResponse(BaseModel):
    """Current state of one campaign.

    ``running`` and ``problems`` are always present because they are what a
    caller acts on. ``units`` is None unless a ``units_limit`` was asked for,
    and is capped at it, with ``truncated`` saying whether any were dropped.
    """

    campaign: CampaignSummary = CampaignSummary()
    running: list[UnitView] = []
    problems: list[UnitView] = []
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
    """One recorded attempt at a unit."""

    state: str = ""
    job_id: str = ""
    install_from: str = ""
    at: str = ""


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
