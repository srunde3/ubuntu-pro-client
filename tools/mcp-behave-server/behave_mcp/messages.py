"""Typed request/response DTOs for the behave MCP server.

Every value the service returns is one of the pydantic models defined here
rather than an ad-hoc dict, so field names and types are validated. The only
places that remain plain dicts are genuinely dynamic payloads: the behave
report status-count maps and the persisted job metadata.

Each response is serialized at the MCP tool boundary via pydantic's own
``model_dump_json()`` / ``model_dump(mode="json")`` - no custom serialization
code. Field declaration order is the wire order, so ``ok`` is declared first
on every response.
"""

from typing import Any

from pydantic import BaseModel

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


# ---------------------------------------------------------------------------
# Response DTOs
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Generic failure response carried by every endpoint's return union."""

    ok: bool = False
    error: str = ""


class ListFeaturesResponse(BaseModel):
    ok: bool = True
    repo_root: str = ""
    features: list[FeatureCatalogEntry] = []


class DescribeFeatureResponse(BaseModel):
    ok: bool = True
    feature_file: str = ""
    title: str = ""
    tags: list[str] = []
    requires_config: list[str] = []
    scenarios: list[ScenarioSummary] = []


class ListDimensionsResponse(BaseModel):
    ok: bool = True
    repo_root: str = ""
    releases: list[DimensionValue] = []
    machine_types: list[DimensionValue] = []


class FindScenariosResponse(BaseModel):
    ok: bool = True
    repo_root: str = ""
    matches: list[ScenarioMatch] = []


class StartScenarioResponse(BaseModel):
    ok: bool = True
    job_id: str = ""
    status: str = "started"
    message: str = ""
    artifacts: Artifacts | None = None


class CapacityExceededResponse(BaseModel):
    ok: bool = False
    status: str = "capacity_exceeded"
    error: str = ""
    capacity: Capacity | None = None


class RunningResponse(BaseModel):
    ok: bool = True
    status: str = "running"
    job_id: str = ""
    recent_output: str = ""
    artifacts: Artifacts | None = None


class CompletedResponse(BaseModel):
    """Completed job. ``ok`` varies with the return code, so it has no default.

    ``summary`` is ``None`` when no parseable JSON report was produced;
    ``recent_output`` is populated only in that same fallback case, and is
    ``None`` (present, but null) otherwise.
    """

    ok: bool
    status: str = "completed"
    job_id: str
    returncode: int | None
    artifacts: Artifacts
    summary: dict[str, dict[str, int]] | None
    failures: list[Failure]
    recent_output: str | None = None


class TimeoutResponse(BaseModel):
    ok: bool = False
    status: str = "timeout"
    job_id: str = ""
    max_wait_seconds: int = 0
    poll_interval_seconds: float = 0.0
    last_status: str = "running"
    recent_output: str = ""
    artifacts: Artifacts | None = None


class LogsResponse(BaseModel):
    ok: bool = True
    job_id: str = ""
    lines: int = 0
    output: str = ""
    output_lines: list[str] = []
    artifacts: Artifacts | None = None


class ArtifactsResponse(BaseModel):
    ok: bool = True
    job_id: str = ""
    artifacts: Artifacts | None = None
    metadata: dict[str, Any] = {}
    exists: ExistsFlags | None = None
