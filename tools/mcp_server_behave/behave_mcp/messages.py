"""Typed request/response DTOs for the behave MCP server."""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Combo(BaseModel):
    """A single valid ``(release, machine_type)`` pair for a scenario."""

    release: str = ""
    machine_type: str = ""


class ExamplesBlock(BaseModel):
    """One ``Examples:`` table within a Scenario Outline, with its own tags."""

    name: str = ""
    tags: list[str] = []
    combos: list[Combo] = []


class ScenarioSummary(BaseModel):
    """A scenario's browse/select metadata (no step text)."""

    name: str = ""
    type: str = ""
    tags: list[str] = []
    requires_config: list[str] = []
    example_columns: list[str] = []
    combos: list[Combo] = []
    examples: list[ExamplesBlock] = []


class FeatureCatalogEntry(BaseModel):
    """Lightweight catalog projection of a feature."""

    path: str = ""
    title: str = ""
    scenario_count: int = 0
    requires_config: list[str] = []
    releases: list[str] = []
    machine_types: list[str] = []


class DimensionValue(BaseModel):
    """A distinct release or machine_type with its scenario count."""

    name: str = ""
    scenario_count: int = 0


class Dimensions(BaseModel):
    """Distinct releases and machine_types across the suite."""

    releases: list[DimensionValue] = []
    machine_types: list[DimensionValue] = []


class ScenarioMatch(BaseModel):
    """A scenario hit with the combos satisfying some filter."""

    feature_file: str = ""
    scenario_name: str = ""
    type: str = ""
    requires_config: list[str] = []
    combos: list[Combo] = []


class Artifacts(BaseModel):
    """On-disk artifact paths for a job."""

    log_dir: str
    stdout_log: str
    json_report: str
    metadata: str


class RepoState(BaseModel):
    """Git state of repo_root when a job started, for test correlation.

    ``commit``/``branch`` are None when repo_root isn't a git checkout (or
    git isn't available); ``dirty`` is None whenever ``commit`` is None.
    """

    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None


class JobStatus(str, Enum):
    """Persisted lifecycle status of a behave job."""

    STARTED = "started"
    COMPLETED = "completed"


class RunStatus(str, Enum):
    """Derived run status of a behave job."""

    RUNNING = "running"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class ScenarioStatus(str, Enum):
    """Classified outcome of a scenario or feature in a behave report."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class JobRecord(BaseModel):
    """Persisted metadata for one behave job."""

    job_id: str = ""
    status: JobStatus | None = None
    started_at: str | None = None
    completed_at: str | None = None
    feature_file: str = ""
    scenario_name: str = ""
    machine_types: list[str] = []
    releases: list[str] = []
    install_from: str = ""
    command: list[str] = []
    repo_root: str = ""
    repo_state: RepoState | None = None
    pid: int | None = None
    returncode: int | None = None
    ok: bool | None = None
    artifacts: Artifacts | None = None


class Capacity(BaseModel):
    """Parallel-job capacity reported when a start is rejected."""

    max_parallel_jobs: int
    running_jobs: int


class Failure(BaseModel):
    """A single failing step extracted from a behave report.

    ``job_id``/``releases``/``machine_types`` are only populated by
    ``summarize_scenario_results``; ``wait_for_completion`` and
    ``get_scenario_artifacts`` leave them at their defaults.
    """

    feature: str
    scenario: str
    step: str
    status: str
    error_message: str
    job_id: str | None = None
    releases: list[str] = []
    machine_types: list[str] = []


class ReportSummary(BaseModel):
    """Parsed behave report: dynamic status counts plus typed failures.

    ``summary`` keeps status-keyed count maps as plain dicts because behave
    statuses are open-ended; ``failures`` is fully enumerated.
    """

    summary: dict[str, dict[str, int]]
    failures: list[Failure]


class ExistsFlags(BaseModel):
    """Existence flags for a job's artifact files."""

    stdout_log: bool
    json_report: bool
    metadata: bool


class ListFeaturesResponse(BaseModel):
    repo_root: str = ""
    features: list[FeatureCatalogEntry] = []


class DescribeFeatureResponse(BaseModel):
    feature_file: str = ""
    title: str = ""
    tags: list[str] = []
    requires_config: list[str] = []
    scenarios: list[ScenarioSummary] = []


class ListDimensionsResponse(BaseModel):
    repo_root: str = ""
    releases: list[DimensionValue] = []
    machine_types: list[DimensionValue] = []


class FindScenariosResponse(BaseModel):
    repo_root: str = ""
    matches: list[ScenarioMatch] = []


class StartScenarioResponse(BaseModel):
    status: Literal["started"] = "started"
    ok: bool = True
    job_id: str = ""
    message: str = ""
    artifacts: Artifacts | None = None
    repo_state: RepoState | None = None


class CapacityExceededResponse(BaseModel):
    status: Literal["capacity_exceeded"] = "capacity_exceeded"
    ok: bool = False
    error: str = ""
    capacity: Capacity | None = None


StartScenarioResult = Annotated[
    Union[StartScenarioResponse, CapacityExceededResponse],
    Field(discriminator="status"),
]


class RunningResponse(BaseModel):
    """Internal: a job still in progress. Never returned at the MCP
    boundary."""

    status: Literal["running"] = "running"
    ok: bool = True
    job_id: str = ""
    recent_output: str = ""
    artifacts: Artifacts | None = None


class CompletedResponse(BaseModel):
    """Completed job. ``ok`` reflects the behave return code, not call success.

    ``summary`` is ``None`` when no parseable JSON report was produced;
    ``recent_output`` is populated only in that same fallback case, and is
    ``None`` (present, but null) otherwise.
    """

    status: Literal["completed"] = "completed"
    ok: bool
    job_id: str
    returncode: int | None
    artifacts: Artifacts
    summary: dict[str, dict[str, int]] | None
    failures: list[Failure]
    recent_output: str | None = None


class TimeoutResponse(BaseModel):
    status: Literal["timeout"] = "timeout"
    ok: bool = False
    job_id: str = ""
    max_wait_seconds: int = 0
    poll_interval_seconds: float = 0.0
    last_status: RunStatus = RunStatus.RUNNING
    recent_output: str = ""
    artifacts: Artifacts | None = None


WaitForCompletionResult = Annotated[
    Union[CompletedResponse, TimeoutResponse],
    Field(discriminator="status"),
]


class LogsResponse(BaseModel):
    job_id: str = ""
    lines: int = 0
    lines_clamped: bool = False
    output: str = ""
    output_lines: list[str] = []
    artifacts: Artifacts | None = None


class ArtifactsResponse(BaseModel):
    job_id: str = ""
    artifacts: Artifacts | None = None
    metadata: JobRecord | None = None
    exists: ExistsFlags | None = None


class JobSummary(BaseModel):
    """One row in a job listing (in-memory or disk-recovered jobs)."""

    job_id: str = ""
    status: RunStatus = RunStatus.UNKNOWN
    ok: bool | None = None
    returncode: int | None = None
    feature_file: str = ""
    scenario_name: str = ""
    machine_types: list[str] = []
    releases: list[str] = []
    started_at: str | None = None
    completed_at: str | None = None
    artifacts: Artifacts | None = None


class ListScenarioJobsResponse(BaseModel):
    repo_root: str = ""
    jobs: list[JobSummary] = []
    total_completed: int = 0
    truncated: bool = False
    limit_clamped: bool = False


class GroupedCount(BaseModel):
    """Scenario-level status counts for one release or machine_type value.

    Every scenario in a job's report is attributed to all of that job's
    declared releases/machine_types -- a job isn't broken down per
    Examples row.
    """

    name: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    unknown: int = 0


class JobCounts(BaseModel):
    """Job-level status totals -- the "how far into it" progress signal."""

    total: int = 0
    running: int = 0
    completed_passed: int = 0
    completed_failed: int = 0
    unknown: int = 0


class SummarizeScenarioResultsResponse(BaseModel):
    repo_root: str = ""
    job_counts: JobCounts = JobCounts()
    by_release: list[GroupedCount] = []
    by_machine_type: list[GroupedCount] = []
    failures: list[Failure] = []
    truncated: bool = False
    limit_clamped: bool = False
    matched_job_ids: list[str] = []
