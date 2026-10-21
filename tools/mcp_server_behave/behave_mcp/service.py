"""Application service orchestrating behave jobs via injected ports."""

import logging
from pathlib import Path
from typing import Callable

from behave_mcp import parser
from behave_mcp.config import Settings
from behave_mcp.domain.jobs import (
    DEFAULT_INSTALL_FROM,
    INSTALL_FROM_ENV_VAR,
    ScenarioCombos,
    UnitMatch,
    build_command,
    classify_job_state,
    job_matches_result_filters,
    match_unit,
    validate_install_from,
    validate_machine_type,
)
from behave_mcp.domain.limits import (
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
from behave_mcp.domain.logs import digest_log, select_log_lines
from behave_mcp.domain.pools import POOL_LIMIT_ENV_VARS, POOL_OF
from behave_mcp.domain.report import summarize_report
from behave_mcp.messages import (
    ArtifactsResponse,
    CapacityExceededResponse,
    CompletedResponse,
    DescribeFeatureResponse,
    ErrorsResponse,
    FindScenariosResponse,
    JobRecord,
    JobResult,
    JobStatus,
    JobSummary,
    KillJobResponse,
    ListDimensionsResponse,
    ListFeaturesResponse,
    ListScenarioJobsResponse,
    LogRegionView,
    LogsResponse,
    LogSummaryView,
    PoolCapacity,
    ResultsResponse,
    RunningResponse,
    RunStatus,
    ScenarioMatch,
    StartScenarioResponse,
    StartScenarioResult,
    StepView,
    TimeoutResponse,
    WaitForCompletionResult,
)
from behave_mcp.ports import (
    FeatureFileReader,
    Job,
    JobRegistry,
    JobResultStore,
    JobResultStoreFactory,
    LogFileOpenError,
    ProcessLauncher,
    ProcessStartError,
    Workspace,
)
from behave_mcp.views import (
    to_catalog_entry,
    to_combo,
    to_dimensions,
    to_scenario_summary,
)

logger = logging.getLogger(__name__)


class BehaveServiceError(Exception):
    """Raised for expected, user-facing failures (bad input, unknown job,
    etc.).

    FastMCP catches any exception raised from a tool function and reports it
    to the MCP client as ``isError: true`` with this message as the text
    content.
    """


class UnknownJobError(BehaveServiceError):
    """Raised when a job_id isn't in the registry and can't be recovered
    from disk."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"Unknown job_id: {job_id}. Call list_scenario_jobs to see "
            "known jobs, or pass repo_root if this job predates a server "
            "restart."
        )


class BehaveService:
    """Coordinates feature discovery and behave job lifecycle.

    All external interactions are delegated to injected ports and callables,
    so tests can supply fakes.
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        settings: Settings,
        feature_reader: FeatureFileReader,
        results: JobResultStoreFactory,
        registry: JobRegistry,
        launcher: ProcessLauncher,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        now_utc: Callable[[], str],
        new_job_id: Callable[[], str],
    ) -> None:
        self._workspace = workspace
        self._settings = settings
        self._feature_reader = feature_reader
        self._results = results
        self._registry = registry
        self._launcher = launcher
        self._monotonic = monotonic
        self._sleep = sleep
        self._now_utc = now_utc
        self._new_job_id = new_job_id

    def list_features(
        self,
        release: str | None = None,
        machine_type: str | None = None,
        tag: str | None = None,
        text: str | None = None,
        repo_root: str = "",
    ) -> ListFeaturesResponse:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        details = self._feature_reader.discover_feature_details(
            resolved_repo_root
        )
        features = [
            to_catalog_entry(parser.catalog_entry(detail))
            for detail in details
            if self._feature_has_match(
                detail,
                release=release,
                machine_type=machine_type,
                tag=tag,
                text=text,
            )
        ]

        return ListFeaturesResponse(
            repo_root=str(resolved_repo_root),
            features=features,
        )

    def describe_feature(
        self, feature_file: str, repo_root: str = ""
    ) -> DescribeFeatureResponse:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        normalized = parser.normalize_feature_file_arg(feature_file)
        details = self._feature_reader.discover_feature_details(
            resolved_repo_root
        )
        for detail in details:
            if detail.path == normalized:
                return DescribeFeatureResponse(
                    feature_file=detail.path,
                    title=detail.title,
                    tags=list(detail.tags),
                    requires_config=list(detail.requires_config),
                    scenarios=[
                        to_scenario_summary(scenario)
                        for scenario in detail.scenarios
                    ],
                )

        raise BehaveServiceError(
            f"Feature is not listed by list_features: {feature_file}"
        )

    def list_dimensions(self, repo_root: str = "") -> ListDimensionsResponse:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        details = self._feature_reader.discover_feature_details(
            resolved_repo_root
        )
        dimensions = to_dimensions(parser.aggregate_dimensions(details))
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
        text: str | None = None,
        repo_root: str = "",
        limit: int = DEFAULT_FIND_LIMIT,
    ) -> FindScenariosResponse:
        if limit <= 0:
            raise BehaveServiceError(
                f"limit must be a positive integer, got {limit}"
            )
        limit_clamped = limit > MAX_FIND_LIMIT
        limit = min(limit, MAX_FIND_LIMIT)
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        details = self._feature_reader.discover_feature_details(
            resolved_repo_root
        )
        matches: list[ScenarioMatch] = []
        for detail in details:
            for scenario in detail.scenarios:
                if not parser.scenario_matches(
                    scenario,
                    detail.tags,
                    release=release,
                    machine_type=machine_type,
                    tag=tag,
                    text=text,
                ):
                    continue
                matches.append(
                    ScenarioMatch(
                        feature_file=detail.path,
                        scenario_name=scenario.name,
                        type=scenario.type,
                        requires_config=list(scenario.requires_config),
                        combos=[
                            to_combo(combo)
                            for combo in parser.filtered_combos(
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
        feature_detail: parser.FeatureDetail,
        *,
        release: str | None,
        machine_type: str | None,
        tag: str | None,
        text: str | None,
    ) -> bool:
        if (
            release is None
            and machine_type is None
            and tag is None
            and text is None
        ):
            return True
        return any(
            parser.scenario_matches(
                scenario,
                feature_detail.tags,
                release=release,
                machine_type=machine_type,
                tag=tag,
                text=text,
            )
            for scenario in feature_detail.scenarios
        )

    def start_scenario(
        self,
        feature_file: str,
        scenario_name: str,
        release: str,
        machine_type: str,
        repo_root: str = "",
        install_from: str = DEFAULT_INSTALL_FROM,
    ) -> StartScenarioResult:
        """Start one job for one unit: a scenario on one release and
        machine type. A selection that matches no unit, or more than one,
        is refused before anything starts."""
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        normalized = parser.normalize_feature_file_arg(feature_file)
        allowed_features = set(
            self._feature_reader.discover_feature_files(resolved_repo_root)
        )
        if normalized not in allowed_features:
            raise BehaveServiceError(
                f"Feature is not listed by list_features: {feature_file}"
            )
        machine_type_error = validate_machine_type(
            machine_type, self._settings.pool_limits
        )
        if machine_type_error:
            raise BehaveServiceError(machine_type_error)
        self._check_unit(
            resolved_repo_root,
            normalized,
            scenario_name,
            release,
            machine_type,
        )

        install_from_error = validate_install_from(install_from)
        if install_from_error:
            raise BehaveServiceError(install_from_error)

        repo_state = self._workspace.repo_state(resolved_repo_root)

        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        results = self._results.bind(log_dir)
        job_id = self._new_job_id()
        write_targets = results.write_targets(job_id)

        pools = frozenset({POOL_OF[machine_type]})
        reserved_job = Job(
            job_id=job_id,
            process_handle=None,
            log_dir=log_dir,
            reserved=True,
            pools=pools,
        )
        reservation = self._registry.try_reserve(
            reserved_job, self._settings.pool_limits
        )
        if not reservation.reserved:
            names = ", ".join(str(usage.pool) for usage in reservation.full)
            env_vars = ", ".join(
                POOL_LIMIT_ENV_VARS[usage.pool] for usage in reservation.full
            )
            return CapacityExceededResponse(
                error=(
                    f"No room in the {names} pool. Wait for an active job "
                    f"to complete, or raise {env_vars}."
                ),
                full_pools=[
                    PoolCapacity(
                        pool=usage.pool.value,
                        limit=usage.limit,
                        running=usage.running,
                    )
                    for usage in reservation.full
                ],
            )

        command = build_command(
            feature_file,
            scenario_name,
            release,
            machine_type,
            write_targets.json_report,
        )
        env = self._workspace.subprocess_env()
        env[INSTALL_FROM_ENV_VAR] = install_from

        try:
            handle = self._launcher.launch(
                command,
                str(resolved_repo_root),
                env,
                write_targets.stdout_log,
            )
        except LogFileOpenError as exc:
            self._registry.release(job_id)
            raise BehaveServiceError(
                f"Failed to open log file for job_id {job_id}: {exc}"
            ) from exc
        except ProcessStartError as exc:
            self._registry.release(job_id)
            raise BehaveServiceError(
                f"Failed to start behave scenario: {exc}"
            ) from exc

        self._registry.register(
            job_id,
            Job(
                job_id=job_id,
                process_handle=handle,
                log_dir=log_dir,
                reserved=False,
                pid=handle.pid,
                pools=pools,
            ),
        )

        artifacts = results.artifacts(job_id)
        artifacts_dict = artifacts.model_dump(mode="json")
        results.write_record(
            job_id,
            JobRecord(
                job_id=job_id,
                status=JobStatus.STARTED,
                started_at=self._now_utc(),
                feature_file=feature_file,
                scenario_name=scenario_name,
                release=release,
                machine_type=machine_type,
                install_from=install_from,
                command=command,
                repo_root=str(resolved_repo_root),
                repo_state=repo_state,
                pid=handle.pid,
                artifacts=artifacts,
            ),
        )
        results.append_event(
            {
                "event": "started",
                "timestamp": self._now_utc(),
                "job_id": job_id,
                "feature_file": feature_file,
                "scenario_name": scenario_name,
                "release": release,
                "machine_type": machine_type,
                "install_from": install_from,
                "repo_state": repo_state.model_dump(mode="json"),
                "artifacts": artifacts_dict,
            },
        )

        return StartScenarioResponse(
            job_id=job_id,
            message="Test started. Call wait_for_scenario_completion.",
            artifacts=artifacts,
            repo_state=repo_state,
        )

    def _check_unit(
        self,
        repo_root: Path,
        feature_file: str,
        scenario_name: str,
        release: str,
        machine_type: str,
    ) -> None:
        scenarios: list[ScenarioCombos] = []
        for detail in self._feature_reader.discover_feature_details(repo_root):
            if detail.path == feature_file:
                scenarios = [
                    (
                        scenario.name,
                        {
                            (combo.release, combo.machine_type)
                            for combo in scenario.combos
                        },
                    )
                    for scenario in detail.scenarios
                ]
        match = match_unit(scenarios, scenario_name, release, machine_type)
        if match == UnitMatch.FOUND:
            return
        unit = f"{scenario_name!r} on {release} {machine_type}"
        refusals = {
            UnitMatch.NO_SCENARIO: (
                f"No scenario named {scenario_name!r} in {feature_file}. "
                "scenario_name must be a scenario's exact name, as "
                "describe_feature lists it."
            ),
            UnitMatch.NOT_ON_COMBO: (
                f"{feature_file} does not run {unit}. describe_feature "
                "lists the releases and machine types each scenario covers."
            ),
            UnitMatch.AMBIGUOUS: (
                f"More than one scenario in {feature_file} runs {unit}, so "
                "a job cannot name exactly one. Rename one of them."
            ),
        }
        raise BehaveServiceError(refusals[match])

    def kill_job(self, job_id: str, repo_root: str = "") -> KillJobResponse:
        """Terminate a running job.

        For a lane that has hung: the campaign's next tick sees the job
        finish and records the unit, rather than waiting on it forever.
        A job recovered from disk has no handle in this process, so there
        is nothing here to signal -- that is reported, not raised.
        """
        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise UnknownJobError(job_id)

        handle = job.process_handle
        if handle is None:
            return KillJobResponse(
                job_id=job_id,
                killed=False,
                message=(
                    "No live handle for this job in this server process; "
                    "it was started before a restart, or has finished."
                ),
            )
        if handle.poll() is not None:
            return KillJobResponse(
                job_id=job_id,
                killed=False,
                message="Job had already finished.",
            )

        handle.terminate()
        return KillJobResponse(
            job_id=job_id, killed=True, message="Termination signalled."
        )

    def job_status(
        self, job_id: str, repo_root: str = ""
    ) -> RunningResponse | CompletedResponse:
        """Return a job's current state without waiting.

        ``wait_for_completion`` requires a positive timeout, so this is what
        a caller polls when it must not block -- a scheduler filling lanes,
        for instance.
        """
        return self._status_payload(job_id, repo_root or None)

    def wait_for_completion(
        self,
        job_id: str,
        max_wait_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = (DEFAULT_WAIT_POLL_INTERVAL_SECONDS),
        repo_root: str = "",
    ) -> WaitForCompletionResult:
        if max_wait_seconds <= 0:
            raise BehaveServiceError("max_wait_seconds must be greater than 0")
        if poll_interval_seconds <= 0:
            raise BehaveServiceError(
                "poll_interval_seconds must be greater than 0"
            )

        deadline = self._monotonic() + max_wait_seconds

        while True:
            payload = self._status_payload(job_id, repo_root or None)

            if isinstance(payload, CompletedResponse):
                return payload

            if self._monotonic() >= deadline:
                return TimeoutResponse(
                    job_id=job_id,
                    max_wait_seconds=max_wait_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    last_status=RunStatus.RUNNING,
                    recent_output=payload.recent_output,
                    artifacts=payload.artifacts,
                )

            self._sleep(poll_interval_seconds)

    def get_logs(
        self,
        job_id: str,
        lines: int = DEFAULT_LOG_LINES,
        repo_root: str = "",
        *,
        pattern: str = "",
        context: int = DEFAULT_LOG_CONTEXT,
        start: int = 0,
    ) -> LogsResponse:
        if lines <= 0:
            raise BehaveServiceError(
                f"lines must be a positive integer, got {lines}"
            )
        if context < 0 or start < 0:
            raise BehaveServiceError(
                "context and start must not be negative, got {} and {}".format(
                    context, start
                )
            )
        lines_clamped = lines > MAX_LOG_LINES
        lines = min(lines, MAX_LOG_LINES)
        context = min(context, MAX_LOG_CONTEXT)

        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise UnknownJobError(job_id)

        results = self._results.bind(job.log_dir)
        if not results.exists(job_id).stdout_log:
            raise BehaveServiceError(
                f"No log file exists for job_id: {job_id}"
            )

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
            raise BehaveServiceError(str(exc)) from exc
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

    def get_errors(self, job_id: str, repo_root: str = "") -> ErrorsResponse:
        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise UnknownJobError(job_id)

        results = self._results.bind(job.log_dir)
        if not results.exists(job_id).stdout_log:
            raise BehaveServiceError(
                f"No log file exists for job_id: {job_id}"
            )

        digest = digest_log(results.read_log_lines(job_id))
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

    def get_artifacts(
        self, job_id: str, repo_root: str = ""
    ) -> ArtifactsResponse:
        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise UnknownJobError(job_id)

        results = self._results.bind(job.log_dir)
        return ArtifactsResponse(
            job_id=job_id,
            artifacts=results.artifacts(job_id),
            metadata=results.read_record(job_id),
            exists=results.exists(job_id),
        )

    def list_jobs(
        self,
        repo_root: str = "",
        limit: int = DEFAULT_JOB_LIST_LIMIT,
    ) -> ListScenarioJobsResponse:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        if limit <= 0:
            raise BehaveServiceError(
                f"limit must be a positive integer, got {limit}"
            )
        limit_clamped = limit > MAX_JOB_LIST_LIMIT
        limit = min(limit, MAX_JOB_LIST_LIMIT)
        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        results = self._results.bind(log_dir)

        in_memory_jobs = {job.job_id: job for job in self._registry.snapshot()}
        disk_job_ids = set(results.list_job_ids())
        disk_only_ids = disk_job_ids - set(in_memory_jobs)

        summaries: list[JobSummary] = []
        for job_id in sorted(set(in_memory_jobs) | disk_job_ids):
            job = in_memory_jobs.get(job_id)
            recovered = job is None
            if job is None:
                job = self._recover_job(job_id, repo_root or None)
                if job is None:
                    continue
            summary = self._job_summary(job_id, job)
            if recovered:
                logger.info(
                    "recovered job %s -> status=%s ok=%s",
                    job_id,
                    summary.status,
                    summary.ok,
                )
            summaries.append(summary)

        summaries.sort(key=lambda summary: summary.started_at or "")
        running = [s for s in summaries if s.status == RunStatus.RUNNING]
        others = [s for s in summaries if s.status != RunStatus.RUNNING]
        trimmed = running + others[-limit:]

        logger.info(
            "listing jobs: %d in-memory, %d disk-only, %d total returned",
            len(in_memory_jobs),
            len(disk_only_ids),
            len(trimmed),
        )

        return ListScenarioJobsResponse(
            repo_root=str(resolved_repo_root),
            jobs=trimmed,
            total_completed=len(others),
            truncated=len(others) > limit,
            limit_clamped=limit_clamped,
        )

    def get_results(
        self,
        job_ids: list[str] | None = None,
        feature_file: str = "",
        scenario_name: str = "",
        release: str = "",
        machine_type: str = "",
        status: str = "",
        limit: int = DEFAULT_RESULTS_LIMIT,
        repo_root: str = "",
    ) -> ResultsResponse:
        status_filter: RunStatus | None = None
        if status:
            try:
                status_filter = RunStatus(status)
            except ValueError:
                allowed = ", ".join(s.value for s in RunStatus)
                raise BehaveServiceError(
                    f"Invalid status filter: {status}. "
                    f"Allowed values: {allowed}"
                ) from None

        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        normalized_feature_file = (
            parser.normalize_feature_file_arg(feature_file)
            if feature_file
            else None
        )
        job_ids_filter = set(job_ids) if job_ids else None
        if limit <= 0:
            raise BehaveServiceError(
                f"limit must be a positive integer, got {limit}"
            )
        limit_clamped = limit > MAX_RESULTS_LIMIT
        limit = min(limit, MAX_RESULTS_LIMIT)

        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        results = self._results.bind(log_dir)
        in_memory_jobs = {job.job_id: job for job in self._registry.snapshot()}
        disk_job_ids = set(results.list_job_ids())

        matched: list[tuple[str, JobSummary, Job]] = []
        for job_id in sorted(set(in_memory_jobs) | disk_job_ids):
            job = in_memory_jobs.get(job_id)
            if job is None:
                job = self._recover_job(job_id, repo_root or None)
                if job is None:
                    continue
            if not job_matches_result_filters(
                results.read_record(job_id),
                job_id=job_id,
                job_ids=job_ids_filter,
                feature_file=normalized_feature_file,
                scenario_name=scenario_name or None,
                release=release or None,
                machine_type=machine_type or None,
            ):
                continue
            summary = self._job_summary(job_id, job)
            if status_filter is not None and summary.status != status_filter:
                continue
            matched.append((job_id, summary, job))

        matched.sort(key=lambda item: item[1].started_at or "", reverse=True)
        return ResultsResponse(
            repo_root=str(resolved_repo_root),
            results=[
                self._job_result(job_id, summary, job)
                for job_id, summary, job in matched[:limit]
            ],
            total=len(matched),
            truncated=len(matched) > limit,
            limit_clamped=limit_clamped,
        )

    def _job_result(
        self, job_id: str, summary: JobSummary, job: Job
    ) -> JobResult:
        report = None
        if summary.status == RunStatus.COMPLETED:
            report_data = self._results.bind(job.log_dir).read_report(job_id)
            if report_data is not None:
                report = summarize_report(report_data)
        return JobResult(
            job_id=job_id,
            status=summary.status.value,
            ok=summary.ok,
            feature_file=summary.feature_file,
            scenario_name=summary.scenario_name,
            release=summary.release,
            machine_type=summary.machine_type,
            summary=None if report is None else report.summary,
            failures=[] if report is None else report.failures,
        )

    def _job_summary(self, job_id: str, job: Job) -> JobSummary:
        results = self._results.bind(job.log_dir)
        record = results.read_record(job_id)

        if job.reserved:
            status, ok, returncode = RunStatus.RUNNING, None, None
        else:
            handle = job.process_handle
            returncode = handle.poll() if handle is not None else None
            report_data = None
            if handle is None or returncode is not None:
                report_data = results.read_report(job_id)
            report = (
                summarize_report(report_data)
                if report_data is not None
                else None
            )
            report_ok = None if report is None else not report.failures

            pid = job.pid if job.pid is not None else record.pid
            pid_alive = False
            if handle is None and pid is not None:
                pid_alive = self._launcher.is_pid_alive(pid)

            classification = classify_job_state(
                has_live_handle=handle is not None,
                returncode=returncode,
                report_present=report is not None,
                report_ok=report_ok,
                pid=pid,
                pid_alive=pid_alive,
            )
            status, ok = classification.status, classification.ok
            if status == RunStatus.COMPLETED:
                record = self._record_completion(
                    job_id, results, returncode=returncode, ok=bool(ok)
                )

        return JobSummary(
            job_id=job_id,
            status=status,
            ok=ok,
            returncode=returncode,
            feature_file=record.feature_file,
            scenario_name=record.scenario_name,
            release=record.release,
            machine_type=record.machine_type,
            started_at=record.started_at,
            completed_at=record.completed_at,
            artifacts=results.artifacts(job_id),
        )

    def _status_payload(
        self, job_id: str, repo_root_override: str | None
    ) -> RunningResponse | CompletedResponse:
        job = self._registry.get(job_id)
        recovered = job is None
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root_override)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise UnknownJobError(job_id)

        handle = job.process_handle
        results = self._results.bind(job.log_dir)

        returncode = handle.poll() if handle is not None else None
        if handle is not None and returncode is None:
            return RunningResponse(
                job_id=job_id,
                recent_output=results.log_tail(
                    job_id, DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=results.artifacts(job_id),
            )

        if handle is not None:
            handle.close()

        report_data = results.read_report(job_id)
        report = (
            summarize_report(report_data) if report_data is not None else None
        )
        report_ok = None if report is None else not report.failures

        pid_alive = False
        if handle is None and job.pid is not None:
            pid_alive = self._launcher.is_pid_alive(job.pid)

        classification = classify_job_state(
            has_live_handle=handle is not None,
            returncode=returncode,
            report_present=report is not None,
            report_ok=report_ok,
            pid=job.pid,
            pid_alive=pid_alive,
        )

        if recovered:
            logger.info(
                "reattached job %s -> status=%s reason=%s pid=%s",
                job_id,
                classification.status,
                classification.reason,
                job.pid,
            )

        if classification.status == RunStatus.RUNNING:
            return RunningResponse(
                job_id=job_id,
                recent_output=results.log_tail(
                    job_id, DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=results.artifacts(job_id),
            )

        ok_value = bool(classification.ok)
        artifacts = results.artifacts(job_id)
        record = self._record_completion(
            job_id, results, returncode=returncode, ok=ok_value
        )
        return CompletedResponse(
            ok=ok_value,
            job_id=job_id,
            returncode=returncode,
            artifacts=artifacts,
            feature_file=record.feature_file,
            scenario_name=record.scenario_name,
            release=record.release,
            machine_type=record.machine_type,
            summary=None if report is None else report.summary,
            failures=[] if report is None else report.failures,
            recent_output=(
                results.log_tail(job_id, DEFAULT_RUNNING_TAIL_LINES)
                if report is None
                else None
            ),
        )

    def _record_completion(
        self,
        job_id: str,
        results: JobResultStore,
        *,
        returncode: int | None,
        ok: bool,
    ) -> JobRecord:
        """Write a job's completion the first time it is seen finished."""
        record = results.read_record(job_id)
        if record.status == JobStatus.COMPLETED:
            return record

        artifacts = results.artifacts(job_id)
        record.job_id = job_id
        record.status = JobStatus.COMPLETED
        record.completed_at = self._now_utc()
        record.returncode = returncode
        record.ok = ok
        record.artifacts = artifacts
        results.write_record(job_id, record)
        results.append_event(
            {
                "event": "completed",
                "timestamp": record.completed_at,
                "job_id": job_id,
                "ok": ok,
                "returncode": returncode,
                "artifacts": artifacts.model_dump(mode="json"),
            },
        )
        return record

    def _recover_job(
        self, job_id: str, repo_root_override: str | None
    ) -> Job | None:
        resolved_repo_root = self._workspace.resolve_repo_root(
            repo_root_override
        )
        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        results = self._results.bind(log_dir)
        exists = results.exists(job_id)
        if not (exists.stdout_log or exists.json_report or exists.metadata):
            logger.warning(
                "job %s not tracked in memory and no disk artifacts found "
                "under %s",
                job_id,
                log_dir,
            )
            return None

        logger.info(
            "job %s not tracked in memory; recovering from disk artifacts "
            "under %s",
            job_id,
            log_dir,
        )
        metadata_record = results.read_record(job_id)
        job = Job(
            job_id=job_id,
            process_handle=None,
            log_dir=log_dir,
            reserved=False,
            pid=metadata_record.pid,
        )
        # Cache the recovery so later calls in this process hit the
        # registry instead of re-reading disk and re-logging every poll.
        self._registry.register(job_id, job)
        return job
