"""Application service orchestrating behave jobs via injected ports."""

import logging
from pathlib import Path
from typing import Any, Callable, ContextManager

from pro_e2e.jobs.domain.catalog import (
    FeatureDetail,
    UnitMatch,
    aggregate_dimensions,
    catalog_entry,
    filtered_combos,
    match_unit,
    normalize_feature_arg,
    scenario_matches,
)
from pro_e2e.jobs.domain.config import (
    CheckoutConfig,
    behave_option_error,
    shown_options,
)
from pro_e2e.jobs.domain.jobs import (
    DEFAULT_INSTALL_FROM,
    Unit,
    interruption,
    job_matches_result_filters,
    job_state,
    validate_install_from,
    validate_machine_type,
)
from pro_e2e.jobs.domain.limits import (
    DEFAULT_FIND_LIMIT,
    DEFAULT_JOB_LIST_LIMIT,
    DEFAULT_LOG_CONTEXT,
    DEFAULT_LOG_LINES,
    DEFAULT_RESULTS_LIMIT,
    DEFAULT_RUNNING_TAIL_LINES,
    DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    MAX_FIND_LIMIT,
    MAX_JOB_LIST_LIMIT,
    MAX_LOG_CONTEXT,
    MAX_LOG_LINES,
    MAX_RESULTS_LIMIT,
)
from pro_e2e.jobs.domain.logs import select_log_lines
from pro_e2e.jobs.domain.pools import (
    DEFAULT_MAX_JOBS,
    POOL_OF,
    Pool,
    max_jobs_error,
)
from pro_e2e.jobs.domain.queue import QueuedJob, plan_launches, queue_position
from pro_e2e.jobs.domain.results import killed_result, start_failure
from pro_e2e.jobs.messages import (
    ArtifactsResponse,
    ConfigResponse,
    DescribeFeatureResponse,
    ErrorsResponse,
    FindScenariosResponse,
    FinishedResponse,
    InterruptedResponse,
    JobListing,
    JobRecord,
    JobRequest,
    JobResult,
    JobState,
    JobStatusResult,
    JobSummary,
    KillJobResponse,
    ListDimensionsResponse,
    ListFeaturesResponse,
    ListJobsResponse,
    LogRegionView,
    LogsResponse,
    LogSummaryView,
    Outcome,
    PoolUsage,
    QueuedResponse,
    ResultsResponse,
    RunJobResponse,
    RunningResponse,
    ScenarioMatch,
    StepView,
)
from pro_e2e.jobs.ports import (
    ConfigStore,
    Framework,
    JobLauncher,
    JobResultStore,
    JobResultStoreFactory,
    JobStartError,
    LanePool,
    ReconcileLock,
    Workspace,
)
from pro_e2e.jobs.views import (
    to_catalog_entry,
    to_combo,
    to_dimensions,
    to_scenario_summary,
)

logger = logging.getLogger(__name__)

# A job in these states has not ended.
ACTIVE_STATES = (JobState.QUEUED, JobState.RUNNING)


def _result_fields(result: JobResult | None) -> dict[str, Any]:
    """A finished job's result, as a response carries it."""
    if result is None:
        return {}
    return dict(
        outcome=result.outcome,
        reason=result.reason,
        instances=result.instances,
        failures=result.failures,
    )


class JobServiceError(Exception):
    """Raised for expected, user-facing failures (bad input, unknown job,
    etc.).

    The MCP server reports it as ``isError: true`` with this message as the
    text content; the CLI prints it and exits 2.
    """


class UnknownJobError(JobServiceError):
    """Raised when no job with this id was ever queued."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"Unknown job_id: {job_id}. Call list_jobs to see " "known jobs."
        )


class JobService:
    """Coordinates feature discovery and behave job lifecycle.

    All external interactions are delegated to injected ports and callables,
    so tests can supply fakes.
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        config: ConfigStore,
        framework: Framework,
        results: JobResultStoreFactory,
        lanes: LanePool,
        launcher: JobLauncher,
        reconcile_lock: ReconcileLock,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        now_utc: Callable[[], str],
        new_job_id: Callable[[], str],
    ) -> None:
        self._workspace = workspace
        self._config = config
        self._framework = framework
        self._results = results
        self._lanes = lanes
        self._launcher = launcher
        self._reconcile_lock = reconcile_lock
        self._monotonic = monotonic
        self._sleep = sleep
        self._now_utc = now_utc
        self._new_job_id = new_job_id

    def list_features(
        self,
        release: str | None = None,
        machine_type: str | None = None,
        tag: str | None = None,
        repo_root: str = "",
    ) -> ListFeaturesResponse:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc

        details = self._framework.catalog(resolved_repo_root)
        features = [
            to_catalog_entry(catalog_entry(detail))
            for detail in details
            if self._feature_has_match(
                detail,
                release=release,
                machine_type=machine_type,
                tag=tag,
            )
        ]

        return ListFeaturesResponse(
            repo_root=str(resolved_repo_root),
            features=features,
        )

    def describe_feature(
        self, feature: str, repo_root: str = ""
    ) -> DescribeFeatureResponse:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc

        normalized = normalize_feature_arg(feature)
        details = self._framework.catalog(resolved_repo_root)
        for detail in details:
            if detail.path == normalized:
                return DescribeFeatureResponse(
                    feature=detail.path,
                    title=detail.title,
                    tags=list(detail.tags),
                    requires_config=list(detail.requires_config),
                    scenarios=[
                        to_scenario_summary(scenario)
                        for scenario in detail.scenarios
                    ],
                )

        raise JobServiceError(
            f"Feature is not listed by list_features: {feature}"
        )

    def list_dimensions(self, repo_root: str = "") -> ListDimensionsResponse:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc

        details = self._framework.catalog(resolved_repo_root)
        dimensions = to_dimensions(aggregate_dimensions(details))
        return ListDimensionsResponse(
            repo_root=str(resolved_repo_root),
            releases=dimensions.releases,
            machine_types=dimensions.machine_types,
        )

    def find_scenarios(
        self,
        release: str | None = None,
        machine_type: str | None = None,
        tag: str | None = None,
        repo_root: str = "",
        limit: int = DEFAULT_FIND_LIMIT,
    ) -> FindScenariosResponse:
        if limit <= 0:
            raise JobServiceError(
                f"limit must be a positive integer, got {limit}"
            )
        limit_clamped = limit > MAX_FIND_LIMIT
        limit = min(limit, MAX_FIND_LIMIT)
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc

        details = self._framework.catalog(resolved_repo_root)
        matches: list[ScenarioMatch] = []
        for detail in details:
            for scenario in detail.scenarios:
                if not scenario_matches(
                    scenario,
                    detail.tags,
                    release=release,
                    machine_type=machine_type,
                    tag=tag,
                ):
                    continue
                matches.append(
                    ScenarioMatch(
                        feature=detail.path,
                        scenario=scenario.name,
                        type=scenario.type,
                        requires_config=list(scenario.requires_config),
                        combos=[
                            to_combo(combo)
                            for combo in filtered_combos(
                                scenario, release, machine_type
                            )
                        ],
                    )
                )

        return FindScenariosResponse(
            repo_root=str(resolved_repo_root),
            matches=matches[:limit],
            total=len(matches),
            truncated=len(matches) > limit,
            limit_clamped=limit_clamped,
        )

    def _feature_has_match(
        self,
        feature_detail: FeatureDetail,
        *,
        release: str | None,
        machine_type: str | None,
        tag: str | None,
    ) -> bool:
        if release is None and machine_type is None and tag is None:
            return True
        return any(
            scenario_matches(
                scenario,
                feature_detail.tags,
                release=release,
                machine_type=machine_type,
                tag=tag,
            )
            for scenario in feature_detail.scenarios
        )

    def run_job(
        self,
        feature: str,
        scenario: str,
        release: str,
        machine_type: str,
        repo_root: str = "",
        install_from: str = DEFAULT_INSTALL_FROM,
    ) -> RunJobResponse:
        """Queue one job for one unit, a scenario on one release and
        machine type, and start it at once if its pool has a free lane. A
        selection that matches no unit, or more than one, is refused before
        anything is queued."""
        job_id = self.queue_job(
            feature,
            scenario,
            release,
            machine_type,
            repo_root=repo_root,
            install_from=install_from,
        )
        self.reconcile()
        payload = self._status_payload(job_id)
        if isinstance(payload, QueuedResponse):
            message = (
                f"Queued, position {payload.queue_position} in the "
                f"{POOL_OF[machine_type]} pool. Call "
                "wait_for_job."
            )
        else:
            message = "Started. Call wait_for_job."
        return RunJobResponse(
            job_id=job_id,
            state=payload.state,
            queue_position=(
                payload.queue_position
                if isinstance(payload, QueuedResponse)
                else None
            ),
            message=message,
            artifacts=payload.artifacts,
        )

    def new_job_id(self) -> str:
        """A fresh job id, for a caller that records it before queuing."""
        return self._new_job_id()

    def queue_job(
        self,
        feature: str,
        scenario: str,
        release: str,
        machine_type: str,
        *,
        repo_root: str = "",
        install_from: str = DEFAULT_INSTALL_FROM,
        job_id: str = "",
        campaign_id: str = "",
        record: Callable[[], None] | None = None,
    ) -> str:
        """Validate one unit and queue its job, without reconciling; return
        the job's id.

        ``record``, when given, is called once the unit is valid and before
        the job is queued: a campaign records its attempt there, so a
        refused unit has no attempt and a queued job always has one.
        """
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc

        normalized = normalize_feature_arg(feature)
        features = {
            detail.path: detail
            for detail in self._framework.catalog(resolved_repo_root)
        }
        if normalized not in features:
            raise JobServiceError(
                f"Feature is not listed by list_features: {feature}"
            )
        machine_type_error = validate_machine_type(
            machine_type, self._read_config().max_jobs
        )
        if machine_type_error:
            raise JobServiceError(machine_type_error)
        self._check_unit(features[normalized], scenario, release, machine_type)

        install_from_error = validate_install_from(install_from)
        if install_from_error:
            raise JobServiceError(install_from_error)

        results = self._results.bind(self._jobs_dir())
        job_id = job_id or self._new_job_id()
        write_targets = results.write_targets(job_id)
        invocation = self._framework.invocation(
            resolved_repo_root,
            Unit(feature, scenario, release, machine_type),
            write_targets.json_report,
        )
        if record is not None:
            record()
        try:
            results.write_request(
                job_id,
                JobRequest(
                    job_id=job_id,
                    queued_at=self._now_utc(),
                    feature=feature,
                    scenario=scenario,
                    release=release,
                    machine_type=machine_type,
                    install_from=install_from,
                    command=invocation.command,
                    instances=invocation.instances,
                    repo_root=str(resolved_repo_root),
                    campaign_id=campaign_id,
                ),
            )
        except OSError as exc:
            raise JobServiceError(f"Could not queue the job: {exc}")
        return job_id

    def reconcile_lock(self) -> ContextManager[None]:
        """Hold the state directory's reconcile lock, re-entrantly: for a
        caller that reconciles more than this service's own queue."""
        return self._reconcile_lock.hold()

    def reconcile(self) -> None:
        """Start queued jobs on free lanes, oldest first in each pool.

        Any process may call this at any time: it holds the state
        directory's reconcile lock, and everything it decides comes from
        files, so the result is the same whoever calls it.
        """
        results = self._results.bind(self._jobs_dir())
        with self._reconcile_lock.hold():
            config = self._read_config()
            busy = {pool: self._lanes.busy(pool) for pool in Pool}
            for job_id in plan_launches(
                self._queued(results), busy, config.max_jobs
            ):
                self._launch(job_id, results, config)

    def _queued(self, results: JobResultStore) -> list[QueuedJob]:
        queued = []
        for job_id in results.list_job_ids():
            flags = results.exists(job_id)
            if flags.record or flags.result:
                continue
            request = results.read_request(job_id)
            if request is None or request.machine_type not in POOL_OF:
                continue
            queued.append(
                QueuedJob(
                    job_id=job_id,
                    pool=POOL_OF[request.machine_type],
                    queued_at=request.queued_at,
                )
            )
        return queued

    def _launch(
        self, job_id: str, results: JobResultStore, config: CheckoutConfig
    ) -> None:
        request = results.read_request(job_id)
        if request is None:
            return
        pool = POOL_OF[request.machine_type]
        lease = self._lanes.take(pool, config.max_jobs.get(pool, 0))
        if lease is None:
            return
        repo = self._workspace.repo_state(Path(request.repo_root))
        env = self._framework.environment(
            self._workspace.subprocess_env(),
            config.behave,
            request.install_from,
        )

        def record() -> None:
            results.write_record(
                job_id,
                JobRecord(
                    job_id=job_id,
                    started_at=self._now_utc(),
                    feature=request.feature,
                    scenario=request.scenario,
                    release=request.release,
                    machine_type=request.machine_type,
                    install_from=request.install_from,
                    command=request.command,
                    campaign_id=request.campaign_id,
                    repo=repo,
                    config=shown_options(config.behave),
                    artifacts=results.artifacts(job_id),
                ),
            )

        try:
            self._launcher.launch(
                job_id, request.command, request.repo_root, env, lease, record
            )
        except JobStartError as exc:
            # Nobody is waiting on a reconcile to raise to: the job ends
            # with the reason, rather than being retried forever.
            results.write_result(
                job_id,
                start_failure(self._now_utc(), f"Could not start: {exc}"),
            )

    def _check_unit(
        self,
        detail: FeatureDetail,
        scenario: str,
        release: str,
        machine_type: str,
    ) -> None:
        feature = detail.path
        match = match_unit(detail, scenario, release, machine_type)
        if match == UnitMatch.FOUND:
            return
        unit = f"{scenario!r} on {release} {machine_type}"
        refusals = {
            UnitMatch.NO_SCENARIO: (
                f"No scenario named {scenario!r} in {feature}. "
                "scenario must be a scenario's exact name, as "
                "describe_feature lists it."
            ),
            UnitMatch.NOT_ON_COMBO: (
                f"{feature} does not run {unit}. describe_feature "
                "lists the releases and machine types each scenario covers."
            ),
            UnitMatch.AMBIGUOUS: (
                f"More than one scenario in {feature} runs {unit}, so "
                "a job cannot name exactly one. Rename one of them."
            ),
        }
        raise JobServiceError(refusals[match])

    def kill_job(self, job_id: str) -> KillJobResponse:
        """Stop a queued or running job, from any process.

        A queued job is recorded as killed before it starts. A running
        job's process passes the kill to its command and waits for it, so the
        job ends and records itself killed. Either way it is interrupted.
        """
        results = self._known(job_id)
        if self._state(job_id, results) == JobState.QUEUED:
            with self._reconcile_lock.hold():
                if self._state(job_id, results) == JobState.QUEUED:
                    results.write_result(
                        job_id,
                        killed_result(
                            self._now_utc(), reason="killed before it started"
                        ),
                    )
                    return KillJobResponse(
                        job_id=job_id,
                        killed=True,
                        message="Killed before it started.",
                    )
        state = self._state(job_id, results)
        if state == JobState.FINISHED:
            message = "Job had already finished."
        elif state == JobState.INTERRUPTED:
            message = "Job had already been interrupted."
        elif self._launcher.kill(job_id):
            return KillJobResponse(
                job_id=job_id, killed=True, message="Kill sent."
            )
        else:
            message = "Job's process could not be reached."
        return KillJobResponse(job_id=job_id, killed=False, message=message)

    def job_status(self, job_id: str) -> JobStatusResult:
        """Return a job's current state without waiting.

        ``wait_for_job`` requires a positive timeout, so this is what
        a caller polls when it must not block -- a scheduler filling lanes,
        for instance.
        """
        return self._status_payload(job_id)

    def wait_for_job(
        self,
        job_id: str,
        max_wait_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = (DEFAULT_WAIT_POLL_INTERVAL_SECONDS),
    ) -> JobStatusResult:
        """Wait for a job to end. A job still queued or running when the
        wait runs out is returned as it stands, with ``timed_out`` set."""
        if max_wait_seconds <= 0:
            raise JobServiceError("max_wait_seconds must be greater than 0")
        if poll_interval_seconds <= 0:
            raise JobServiceError(
                "poll_interval_seconds must be greater than 0"
            )

        deadline = self._monotonic() + max_wait_seconds

        while True:
            payload = self._status_payload(job_id)

            if not isinstance(payload, (QueuedResponse, RunningResponse)):
                return payload

            if self._monotonic() >= deadline:
                payload.timed_out = True
                return payload

            self._sleep(poll_interval_seconds)

    def job_logs(
        self,
        job_id: str,
        lines: int = DEFAULT_LOG_LINES,
        *,
        pattern: str = "",
        context: int = DEFAULT_LOG_CONTEXT,
        start: int = 0,
    ) -> LogsResponse:
        if lines <= 0:
            raise JobServiceError(
                f"lines must be a positive integer, got {lines}"
            )
        if context < 0 or start < 0:
            raise JobServiceError(
                "context and start must not be negative, got {} and {}".format(
                    context, start
                )
            )
        lines_clamped = lines > MAX_LOG_LINES
        lines = min(lines, MAX_LOG_LINES)
        context = min(context, MAX_LOG_CONTEXT)

        results = self._known(job_id)
        if not results.exists(job_id).stdout_log:
            raise JobServiceError(f"No log file exists for job_id: {job_id}")

        log_lines = results.read_log_lines(job_id)
        try:
            selection = select_log_lines(
                log_lines,
                pattern=pattern,
                context=context,
                start=start,
                limit=lines,
            )
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc
        return LogsResponse(
            job_id=job_id,
            total_lines=len(log_lines),
            first_line=selection.first_line,
            last_line=selection.last_line,
            matches=selection.matches,
            truncated=selection.truncated,
            lines_clamped=lines_clamped,
            text=selection.text,
            log_path=results.artifacts(job_id).stdout_log,
        )

    def job_errors(self, job_id: str) -> ErrorsResponse:
        results = self._known(job_id)
        if not results.exists(job_id).stdout_log:
            raise JobServiceError(f"No log file exists for job_id: {job_id}")

        digest = self._framework.digest_log(results.read_log_lines(job_id))
        return ErrorsResponse(
            job_id=job_id,
            total_lines=digest.total_lines,
            finished=digest.finished,
            errors=[
                LogRegionView(
                    kind=region.kind,
                    first_line=region.first_line,
                    last_line=region.last_line,
                    step=(
                        StepView(line=region.step.line, text=region.step.text)
                        if region.step
                        else None
                    ),
                    exception=region.exception,
                    text=region.text,
                )
                for region in digest.errors
            ],
            errors_total=digest.errors_total,
            summary=(
                LogSummaryView(
                    first_line=digest.summary.first_line,
                    last_line=digest.summary.last_line,
                    text=digest.summary.text,
                )
                if digest.summary
                else None
            ),
            tail=digest.tail,
            log_path=results.artifacts(job_id).stdout_log,
        )

    def job_artifacts(self, job_id: str) -> ArtifactsResponse:
        results = self._known(job_id)
        return ArtifactsResponse(
            job_id=job_id,
            artifacts=results.artifacts(job_id),
            request=results.read_request(job_id),
            record=(
                results.read_record(job_id)
                if results.exists(job_id).record
                else None
            ),
            result=results.read_result(job_id),
            exists=results.exists(job_id),
        )

    def list_jobs(
        self, limit: int = DEFAULT_JOB_LIST_LIMIT
    ) -> ListJobsResponse:
        if limit <= 0:
            raise JobServiceError(
                f"limit must be a positive integer, got {limit}"
            )
        limit_clamped = limit > MAX_JOB_LIST_LIMIT
        limit = min(limit, MAX_JOB_LIST_LIMIT)
        results = self._results.bind(self._jobs_dir())

        summaries = [
            self._job_listing(job_id, results)
            for job_id in results.list_job_ids()
        ]
        summaries.sort(key=lambda summary: summary.queued_at)
        active = [s for s in summaries if s.state in ACTIVE_STATES]
        others = [s for s in summaries if s.state not in ACTIVE_STATES]
        trimmed = active + others[-limit:]

        return ListJobsResponse(
            jobs=trimmed,
            pools=self.pool_usage(),
            total_ended=len(others),
            truncated=len(others) > limit,
            limit_clamped=limit_clamped,
        )

    def job_results(
        self,
        job_ids: list[str] | None = None,
        feature: str = "",
        scenario: str = "",
        release: str = "",
        machine_type: str = "",
        state: str = "",
        limit: int = DEFAULT_RESULTS_LIMIT,
    ) -> ResultsResponse:
        state_filter: JobState | None = None
        if state:
            try:
                state_filter = JobState(state)
            except ValueError:
                allowed = ", ".join(s.value for s in JobState)
                raise JobServiceError(
                    f"Invalid state filter: {state}. "
                    f"Allowed values: {allowed}"
                ) from None

        normalized_feature = (
            normalize_feature_arg(feature) if feature else None
        )
        job_ids_filter = set(job_ids) if job_ids else None
        if limit <= 0:
            raise JobServiceError(
                f"limit must be a positive integer, got {limit}"
            )
        limit_clamped = limit > MAX_RESULTS_LIMIT
        limit = min(limit, MAX_RESULTS_LIMIT)

        results = self._results.bind(self._jobs_dir())

        matched: list[tuple[str, JobListing]] = []
        for job_id in results.list_job_ids():
            request = results.read_request(job_id)
            if request is None or not job_matches_result_filters(
                request,
                job_id=job_id,
                job_ids=job_ids_filter,
                feature=normalized_feature,
                scenario=scenario or None,
                release=release or None,
                machine_type=machine_type or None,
            ):
                continue
            summary = self._job_listing(job_id, results)
            if state_filter is not None and summary.state != state_filter:
                continue
            matched.append((job_id, summary))

        matched.sort(key=lambda item: item[1].queued_at, reverse=True)
        return ResultsResponse(
            results=[
                self._job_result(job_id, summary, results)
                for job_id, summary in matched[:limit]
            ],
            total=len(matched),
            truncated=len(matched) > limit,
            limit_clamped=limit_clamped,
        )

    def _job_result(
        self, job_id: str, listing: JobListing, results: JobResultStore
    ) -> JobSummary:
        result = (
            results.read_result(job_id)
            if listing.state == JobState.FINISHED
            else None
        )
        return JobSummary(
            job_id=job_id,
            state=listing.state,
            feature=listing.feature,
            scenario=listing.scenario,
            release=listing.release,
            machine_type=listing.machine_type,
            **_result_fields(result),
        )

    def _job_listing(self, job_id: str, results: JobResultStore) -> JobListing:
        request = results.read_request(job_id) or JobRequest()
        record = results.read_record(job_id)
        result = results.read_result(job_id)
        state = self._state(job_id, results, result)
        return JobListing(
            job_id=job_id,
            state=state,
            interruption=(
                interruption(result) if state == JobState.INTERRUPTED else None
            ),
            outcome=result.outcome if result is not None else None,
            returncode=result.returncode if result is not None else None,
            feature=request.feature,
            scenario=request.scenario,
            release=request.release,
            machine_type=request.machine_type,
            campaign_id=request.campaign_id,
            queued_at=request.queued_at,
            started_at=record.started_at,
            finished_at=result.finished_at if result is not None else None,
            artifacts=results.artifacts(job_id),
        )

    def _status_payload(self, job_id: str) -> JobStatusResult:
        results = self._known(job_id)
        result = results.read_result(job_id)
        state = self._state(job_id, results, result)
        artifacts = results.artifacts(job_id)

        if state == JobState.QUEUED:
            return QueuedResponse(
                job_id=job_id,
                queue_position=queue_position(job_id, self._queued(results)),
                artifacts=artifacts,
            )
        if state == JobState.RUNNING:
            return RunningResponse(
                job_id=job_id,
                started_at=results.read_record(job_id).started_at or "",
                recent_output=results.log_tail(
                    job_id, DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=artifacts,
            )

        request = results.read_request(job_id) or JobRequest()
        unit: dict[str, Any] = dict(
            feature=request.feature,
            scenario=request.scenario,
            release=request.release,
            machine_type=request.machine_type,
        )
        if (
            state == JobState.INTERRUPTED
            or result is None
            or result.outcome is None
        ):
            return InterruptedResponse(
                job_id=job_id,
                interruption=interruption(result),
                artifacts=artifacts,
                recent_output=results.log_tail(
                    job_id, DEFAULT_RUNNING_TAIL_LINES
                ),
                **unit,
            )

        return FinishedResponse(
            job_id=job_id,
            returncode=result.returncode,
            artifacts=artifacts,
            recent_output=(
                results.log_tail(job_id, DEFAULT_RUNNING_TAIL_LINES)
                if result.outcome == Outcome.ERROR
                else None
            ),
            **unit,
            **_result_fields(result),
        )

    def _known(self, job_id: str) -> JobResultStore:
        """The store holding ``job_id``, which must have been queued."""
        results = self._results.bind(self._jobs_dir())
        if not results.exists(job_id).request:
            raise UnknownJobError(job_id)
        return results

    def _state(
        self,
        job_id: str,
        results: JobResultStore,
        result: JobResult | None = None,
    ) -> JobState:
        if result is None:
            result = results.read_result(job_id)
        launched = results.exists(job_id).record
        return job_state(
            result=result,
            launched=launched,
            lock_held=(
                result is None
                and launched
                and self._launcher.is_running(job_id)
            ),
        )

    def _read_config(self) -> CheckoutConfig:
        try:
            return self._config.read()
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc

    def read_config(self) -> CheckoutConfig:
        """The checkout's config, secrets included: for front-ends to
        show, never to return whole."""
        return self._read_config()

    def pool_usage(self) -> list[PoolUsage]:
        max_jobs = self._read_config().max_jobs
        return [
            PoolUsage(
                pool=pool.value,
                max_jobs=max_jobs.get(pool, 0),
                busy=self._lanes.busy(pool),
            )
            for pool in Pool
        ]

    def config_view(self) -> ConfigResponse:
        return ConfigResponse(
            pools=self.pool_usage(),
            behave=shown_options(self._read_config().behave),
        )

    def set_config(self, key: str, value: str | None) -> ConfigResponse:
        """Set ``key`` to ``value``, or unset it when ``value`` is None.

        Keys are ``max_jobs.<pool>`` and ``behave.<option>``. Unsetting
        max jobs returns the pool to its default. The change applies to
        every job that starts after it.
        """
        section, _, name = key.partition(".")
        config = self._read_config()
        if section == "max_jobs":
            try:
                pool = Pool(name)
            except ValueError:
                raise JobServiceError(
                    f"Unknown pool in {key}. Pools: "
                    + ", ".join(p.value for p in Pool)
                ) from None
            max_jobs = dict(config.max_jobs)
            if value is None:
                max_jobs[pool] = DEFAULT_MAX_JOBS[pool]
            else:
                try:
                    count: object = int(value)
                except ValueError:
                    count = value
                error = max_jobs_error(pool, count)
                if error:
                    raise JobServiceError(error)
                max_jobs[pool] = int(value)
            config = CheckoutConfig(max_jobs=max_jobs, behave=config.behave)
        elif section == "behave":
            error = behave_option_error(name)
            if error:
                raise JobServiceError(error)
            behave = dict(config.behave)
            if value is None:
                behave.pop(name, None)
            else:
                behave[name] = value
            config = CheckoutConfig(max_jobs=config.max_jobs, behave=behave)
        else:
            raise JobServiceError(
                f"Unknown config key {key!r}: use max_jobs.<pool> or "
                "behave.<option>."
            )
        self._config.write(config)
        return self.config_view()

    def _jobs_dir(self) -> Path:
        try:
            return self._workspace.resolve_jobs_dir()
        except ValueError as exc:
            raise JobServiceError(str(exc)) from exc
