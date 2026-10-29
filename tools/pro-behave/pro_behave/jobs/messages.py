"""Typed request/response DTOs for the job service."""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


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
    result: str
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
    """A job's state, read off its files and locks."""

    # Waiting for a lane in its pool.
    QUEUED = "queued"
    RUNNING = "running"
    # The job ended with a result: its command ran to the end, or could not
    # start.
    FINISHED = "finished"
    # The job ended without a result: it was killed, or its process died.
    INTERRUPTED = "interrupted"


class Interruption(str, Enum):
    """Why an interrupted job has no result."""

    KILLED = "killed"
    # Its process died without writing a result: a crash, a hard kill, a
    # reboot.
    DIED = "died"


class Outcome(str, Enum):
    """What a finished job established for its unit."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    # The run established nothing about the product: it could not start,
    # left no readable report, or the test machinery broke.
    ERROR = "error"


class InstanceStatus(str, Enum):
    """How one of a unit's instances went."""

    PASSED = "passed"
    # A step failed.
    FAILED = "failed"
    SKIPPED = "skipped"
    # The test machinery broke: a hook, a cleanup, an undefined step, or a
    # status the framework's adapter does not recognise.
    ERROR = "error"


class Instance(BaseModel):
    """One execution inside a unit. ``id`` is unique in the checkout;
    ``name`` is what the framework calls it."""

    id: str
    name: str


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
    # The unit's own instances, as the framework named them when queued.
    instances: list[Instance] = []
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


class InstanceResult(BaseModel):
    """How one of the unit's instances went. ``reason`` says why it was
    skipped, or how the test machinery broke."""

    id: str
    name: str
    status: InstanceStatus
    reason: str = ""


class Failure(BaseModel):
    """One failing step of an instance."""

    instance: str
    step: str
    status: str
    error_message: str


class JobResult(BaseModel):
    """How one job ended. Written once, as ``result.json``.

    A killed job has no outcome; every other job has one. ``reason`` says
    why the outcome is ``error``, or why a job was killed before it started.
    """

    finished_at: str = ""
    returncode: int | None = None
    killed: bool = False
    outcome: Outcome | None = None
    reason: str = ""
    instances: list[InstanceResult] = []
    failures: list[Failure] = []

    @model_validator(mode="after")
    def _killed_means_no_outcome(self) -> "JobResult":
        if self.killed != (self.outcome is None):
            raise ValueError("a result has an outcome exactly when not killed")
        return self


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


class ExistsFlags(BaseModel):
    """Which of a job's files exist."""

    request: bool
    record: bool
    result: bool
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


class JobSummary(BaseModel):
    """What came of one job: what ran, and its result once it finished.

    ``outcome`` is None until the job finishes, and for an interrupted job.
    """

    job_id: str = ""
    state: JobState = JobState.RUNNING
    feature: str = ""
    scenario: str = ""
    release: str = ""
    machine_type: str = ""
    outcome: Outcome | None = None
    reason: str = ""
    instances: list[InstanceResult] = []
    failures: list[Failure] = []


class FinishedResponse(JobSummary):
    """A job that ended with a result. ``recent_output`` is the log's tail
    when the outcome is ``error``, and None otherwise."""

    state: Literal[JobState.FINISHED] = JobState.FINISHED
    outcome: Outcome
    returncode: int | None
    artifacts: Artifacts
    recent_output: str | None = None


class InterruptedResponse(JobSummary):
    """A job that ended without a result: it was killed, or its process
    died. Nothing it did counts."""

    state: Literal[JobState.INTERRUPTED] = JobState.INTERRUPTED
    interruption: Interruption = Interruption.DIED
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
    result: JobResult | None = None
    exists: ExistsFlags | None = None


class JobListing(BaseModel):
    """One row in a job listing."""

    job_id: str = ""
    state: JobState = JobState.RUNNING
    interruption: Interruption | None = None
    outcome: Outcome | None = None
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

    results: list[JobSummary] = []
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
