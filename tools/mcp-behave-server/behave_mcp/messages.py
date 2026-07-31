"""Typed request/response DTOs for the behave MCP server.
"""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared data DTOs
# ---------------------------------------------------------------------------


class Combo(BaseModel):
    """A single valid ``(release, machine_type)`` pair for a scenario."""

    release: str
    machine_type: str


class ScenarioSummary(BaseModel):
    """A scenario's browse/select metadata (no step text)."""

    name: str
    type: str
    tags: list[str]
    requires_config: list[str]
    example_columns: list[str]
    combos: list[Combo]


class FeatureDetail(BaseModel):
    """Full parsed metadata for one feature file."""

    path: str
    title: str
    tags: list[str]
    requires_config: list[str]
    scenarios: list[ScenarioSummary]


class FeatureCatalogEntry(BaseModel):
    """Lightweight catalog projection of a feature for ``list_features``."""

    path: str
    title: str
    scenario_count: int
    requires_config: list[str]
    releases: list[str]
    machine_types: list[str]


class DimensionValue(BaseModel):
    """A distinct release or machine_type with its scenario count."""

    name: str
    scenario_count: int


class Dimensions(BaseModel):
    """Distinct releases and machine_types across the suite."""

    releases: list[DimensionValue]
    machine_types: list[DimensionValue]


class ScenarioMatch(BaseModel):
    """A ``find_scenarios`` hit with the combos satisfying the filters."""

    feature_file: str
    scenario_name: str
    type: str
    requires_config: list[str]
    combos: list[Combo]


class Artifacts(BaseModel):
    """On-disk artifact paths for a job."""

    log_dir: str
    stdout_log: str
    json_report: str
    metadata: str


class Capacity(BaseModel):
    """Parallel-job capacity reported when a start is rejected."""

    max_parallel_jobs: int
    running_jobs: int


class Failure(BaseModel):
    """A single failing step extracted from a behave report."""

    feature: str
    scenario: str
    step: str
    status: str
    error_message: str


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
    """Internal: a job still in progress. Never returned at the MCP boundary."""

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
    last_status: str = "running"
    recent_output: str = ""
    artifacts: Artifacts | None = None


WaitForCompletionResult = Annotated[
    Union[CompletedResponse, TimeoutResponse],
    Field(discriminator="status"),
]


class LogsResponse(BaseModel):
    job_id: str = ""
    lines: int = 0
    output: str = ""
    output_lines: list[str] = []
    artifacts: Artifacts | None = None


class ArtifactsResponse(BaseModel):
    job_id: str = ""
    artifacts: Artifacts | None = None
    metadata: dict[str, Any] = {}
    exists: ExistsFlags | None = None
