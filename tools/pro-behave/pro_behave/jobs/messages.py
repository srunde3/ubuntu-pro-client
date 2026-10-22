"""Typed request/response DTOs for the job service."""

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

    feature: str = ""
    scenario: str = ""
    type: str = ""
    requires_config: list[str] = []
    combos: list[Combo] = []


class Artifacts(BaseModel):
    """On-disk paths for a job: its directory and the files in it."""

    job_dir: str
    request: str
    record: str
    exit: str
    stdout_log: str
    json_report: str


class RepoState(BaseModel):
    """The checkout a job ran in, and its git state when the job started.

    ``commit``/``branch`` are None when ``root`` isn't a git checkout (or
    git isn't available); ``dirty`` is None whenever ``commit`` is None.
    """

    root: str = ""
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None


class JobState(str, Enum):
    """A behave job's state, read off its files and locks."""

    # Waiting for a lane in its pool.
    QUEUED = "queued"
    RUNNING = "running"
    # tox ran to its end; the exit says how it went.
    FINISHED = "finished"
    # The job ended without a result: it was killed, or its process died.
    INTERRUPTED = "interrupted"


class Interruption(str, Enum):
    """Why an interrupted job has no result."""

    KILLED = "killed"
    # Its process died without writing an exit: a crash, a hard kill, a
    # reboot.
    DIED = "died"


class ScenarioStatus(str, Enum):
    """Classified outcome of a scenario or feature in a behave report."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class JobRequest(BaseModel):
    """What one behave job is to run. Written once, when it is queued.

    The command is built and validated then, so the job launches exactly
    what was asked for, whichever process launches it.
    """

    job_id: str = ""
    queued_at: str = ""
    feature: str = ""
    scenario: str = ""
    release: str = ""
    machine_type: str = ""
    install_from: str = ""
    command: list[str] = []
    repo_root: str = ""
    # The campaign this job is an attempt for; empty for a one-off job.
    campaign_id: str = ""


class JobRecord(BaseModel):
    """What one behave job ran with. Written once, at launch.

    ``config`` is every behave option in force, each secret shown only as
    set.
    """

    job_id: str = ""
    started_at: str | None = None
    feature: str = ""
    scenario: str = ""
    release: str = ""
    machine_type: str = ""
    install_from: str = ""
    command: list[str] = []
    campaign_id: str = ""
    repo: RepoState = RepoState()
    config: dict[str, str] = {}
    artifacts: Artifacts | None = None


class JobExit(BaseModel):
    """How one behave job ended. Written once, by its job process.

    ``ok`` is true for a zero exit that nobody killed. ``error`` says why
    the job never ran, when tox could not be started at all.
    """

    finished_at: str = ""
    returncode: int | None = None
    ok: bool = False
    killed: bool = False
    error: str = ""


class PoolUsage(BaseModel):
    """How many of a pool's lanes are busy, out of its max jobs."""

    pool: str
    max_jobs: int
    busy: int


class ConfigResponse(BaseModel):
    """The checkout's config: each pool's max jobs and busy lanes, and the
    behave options every job runs with, secrets shown only as set."""

    pools: list[PoolUsage] = []
    behave: dict[str, str] = {}


class Failure(BaseModel):
    """A single failing step extracted from a behave report."""

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
    """Which of a job's files exist."""

    request: bool
    record: bool
    exit: bool
    stdout_log: bool
    json_report: bool


class ListFeaturesResponse(BaseModel):
    repo_root: str = ""
    features: list[FeatureCatalogEntry] = []


class DescribeFeatureResponse(BaseModel):
    feature: str = ""
    title: str = ""
    tags: list[str] = []
    requires_config: list[str] = []
    scenarios: list[ScenarioSummary] = []


class ListDimensionsResponse(BaseModel):
    repo_root: str = ""
    releases: list[DimensionValue] = []
    machine_types: list[DimensionValue] = []


class FindScenariosResponse(BaseModel):
    """Matches in catalog order, capped at ``limit``; ``total`` counts them
    all and ``truncated`` says some were left out."""

    repo_root: str = ""
    matches: list[ScenarioMatch] = []
    total: int = 0
    truncated: bool = False
    limit_clamped: bool = False


class RunJobResponse(BaseModel):
    """A job, queued and started at once when its pool had a free lane."""

    job_id: str = ""
    state: JobState = JobState.QUEUED
    queue_position: int | None = None
    message: str = ""
    artifacts: Artifacts | None = None


class QueuedResponse(BaseModel):
    """A job waiting for a lane. ``queue_position`` 1 is next in its pool.
    ``timed_out`` is set when a wait ended before the job did."""

    state: Literal[JobState.QUEUED] = JobState.QUEUED
    job_id: str = ""
    queue_position: int | None = None
    timed_out: bool = False
    artifacts: Artifacts | None = None


class RunningResponse(BaseModel):
    """A job in progress. ``timed_out`` is set when a wait ended before the
    job did."""

    state: Literal[JobState.RUNNING] = JobState.RUNNING
    job_id: str = ""
    started_at: str = ""
    recent_output: str = ""
    timed_out: bool = False
    artifacts: Artifacts | None = None


class JobResult(BaseModel):
    """What came of one job: what ran, how it ended, and what failed.

    ``ok`` reflects the behave return code. ``summary`` is behave's own
    counts (features, scenarios, steps by status) and is None until the job
    finishes, or when no parseable report was produced.
    """

    job_id: str = ""
    state: JobState = JobState.RUNNING
    ok: bool | None = None
    feature: str = ""
    scenario: str = ""
    release: str = ""
    machine_type: str = ""
    summary: dict[str, dict[str, int]] | None = None
    failures: list[Failure] = []


class FinishedResponse(JobResult):
    """A job that ran to its end. ``recent_output`` is populated only when
    no parseable report was produced, and is None otherwise."""

    state: Literal[JobState.FINISHED] = JobState.FINISHED
    ok: bool
    returncode: int | None
    # Why tox never ran, when it could not be started.
    error: str = ""
    artifacts: Artifacts
    recent_output: str | None = None


class InterruptedResponse(JobResult):
    """A job that ended without a result: it was killed, or its process
    died. Nothing it did counts."""

    state: Literal[JobState.INTERRUPTED] = JobState.INTERRUPTED
    ok: bool = False
    reason: Interruption = Interruption.DIED
    artifacts: Artifacts
    recent_output: str = ""


JobStatusResult = Annotated[
    Union[
        QueuedResponse, RunningResponse, FinishedResponse, InterruptedResponse
    ],
    Field(discriminator="state"),
]


class LogsResponse(BaseModel):
    """Part of a job's stdout log, every line prefixed with its number.

    ``first_line``/``last_line`` bound what ``text`` covers and are None
    when it is empty. ``matches`` counts every line the pattern matched
    from ``start`` on; ``truncated`` says the ``lines`` budget cut some of
    what was asked for.
    """

    job_id: str = ""
    total_lines: int = 0
    first_line: int | None = None
    last_line: int | None = None
    matches: int = 0
    truncated: bool = False
    lines_clamped: bool = False
    text: str = ""
    log_path: str = ""


class StepView(BaseModel):
    """The failing step line a log region sits under."""

    line: int = 0
    text: str = ""


class LogRegionView(BaseModel):
    """One failure as the log shows it.

    ``kind`` is traceback, hook_error or assert. ``exception`` names what
    went wrong -- for a chained traceback, the last one raised. ``text`` is
    the region itself, with long middles and long lines elided.
    """

    kind: str = ""
    first_line: int = 0
    last_line: int = 0
    step: StepView | None = None
    exception: str = ""
    text: str = ""


class LogSummaryView(BaseModel):
    """behave's own closing block: failing scenarios and the counts."""

    first_line: int = 0
    last_line: int = 0
    text: str = ""


class ErrorsResponse(BaseModel):
    """A job's failures as its log shows them, in order.

    ``errors`` is capped; ``errors_total`` says how many there were.
    ``finished`` is whether behave reached its summary. ``tail`` and
    ``log_path`` are there for a run the parser has nothing to say about
    -- a tox or pip failure before behave, a killed job.
    """

    job_id: str = ""
    total_lines: int = 0
    finished: bool = False
    errors: list[LogRegionView] = []
    errors_total: int = 0
    summary: LogSummaryView | None = None
    tail: str = ""
    log_path: str = ""


class ArtifactsResponse(BaseModel):
    job_id: str = ""
    artifacts: Artifacts | None = None
    request: JobRequest | None = None
    record: JobRecord | None = None
    exit: JobExit | None = None
    exists: ExistsFlags | None = None


class JobListing(BaseModel):
    """One row in a job listing."""

    job_id: str = ""
    state: JobState = JobState.RUNNING
    reason: Interruption | None = None
    ok: bool | None = None
    returncode: int | None = None
    feature: str = ""
    scenario: str = ""
    release: str = ""
    machine_type: str = ""
    campaign_id: str = ""
    queued_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    artifacts: Artifacts | None = None


class ListJobsResponse(BaseModel):
    jobs: list[JobListing] = []
    pools: list[PoolUsage] = []
    total_ended: int = 0
    truncated: bool = False
    limit_clamped: bool = False


class ResultsResponse(BaseModel):
    """One result per matching job, most recently started first."""

    results: list[JobResult] = []
    total: int = 0
    truncated: bool = False
    limit_clamped: bool = False


class KillJobResponse(BaseModel):
    """Outcome of killing a job.

    ``killed`` is False when there was nothing to stop: the job had already
    ended, or its process could not be reached.
    """

    job_id: str = ""
    killed: bool = False
    message: str = ""
