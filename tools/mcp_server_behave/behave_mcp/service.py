"""Application service orchestrating behave jobs via injected ports."""

import logging
from typing import Any, Callable

from behave_mcp import domain, parser
from behave_mcp.config import Settings
from behave_mcp.messages import (
    ArtifactsResponse,
    Capacity,
    CapacityExceededResponse,
    CompletedResponse,
    DescribeFeatureResponse,
    ErrorsResponse,
    Failure,
    FindScenariosResponse,
    JobCounts,
    JobRecord,
    JobStatus,
    JobSummary,
    KillJobResponse,
    ListDimensionsResponse,
    ListFeaturesResponse,
    ListScenarioJobsResponse,
    LogRegionView,
    LogsResponse,
    LogSummaryView,
    RunningResponse,
    RunStatus,
    ScenarioMatch,
    StartScenarioResponse,
    StartScenarioResult,
    StepView,
    SummarizeScenarioResultsResponse,
    TimeoutResponse,
    WaitForCompletionResult,
)
from behave_mcp.ports import (
    FeatureFileReader,
    Job,
    JobRegistry,
    JobResultStoreFactory,
    LogFileOpenError,
    ProcessLauncher,
    ProcessStartError,
    Workspace,
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
            domain.to_catalog_entry(parser.catalog_entry(detail))
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
                        domain.to_scenario_summary(scenario)
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
        dimensions = domain.to_dimensions(parser.aggregate_dimensions(details))
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
    ) -> FindScenariosResponse:
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
                            domain.to_combo(combo)
                            for combo in parser.filtered_combos(
                                scenario, release, machine_type
                            )
                        ],
                    )
                )

        return FindScenariosResponse(
            repo_root=str(resolved_repo_root),
            matches=matches,
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
        machine_types: list[str],
        scenario_name: str = "",
        releases: list[str] | None = None,
        repo_root: str = "",
        install_from: str = domain.DEFAULT_INSTALL_FROM,
    ) -> StartScenarioResult:
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

        machine_type_error = domain.validate_machine_types(
            machine_types, self._settings.allow_cloud_machine_types
        )
        if machine_type_error:
            raise BehaveServiceError(machine_type_error)

        install_from_error = domain.validate_install_from(install_from)
        if install_from_error:
            raise BehaveServiceError(install_from_error)

        repo_state = self._workspace.repo_state(resolved_repo_root)

        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        results = self._results.bind(log_dir)
        job_id = self._new_job_id()
        write_targets = results.write_targets(job_id)

        reserved_job = Job(
            job_id=job_id,
            process_handle=None,
            log_dir=log_dir,
            reserved=True,
        )
        reservation = self._registry.try_reserve(
            reserved_job, self._settings.max_parallel_jobs
        )
        if not reservation.reserved:
            return CapacityExceededResponse(
                error=(
                    "Maximum parallel behave jobs reached. "
                    f"Set {domain.MAX_PARALLEL_JOBS_ENV_VAR} to a "
                    "higher value or wait for an active job to complete."
                ),
                capacity=Capacity(
                    max_parallel_jobs=reservation.max_parallel,
                    running_jobs=reservation.running_jobs,
                ),
            )

        command = domain.build_command(
            feature_file,
            machine_types,
            scenario_name,
            releases,
            write_targets.json_report,
        )
        env = self._workspace.subprocess_env()
        env[domain.INSTALL_FROM_ENV_VAR] = install_from

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
                machine_types=machine_types,
                releases=releases or [],
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
                "machine_types": machine_types,
                "releases": releases or [],
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
        max_wait_seconds: int = domain.DEFAULT_WAIT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = (
            domain.DEFAULT_WAIT_POLL_INTERVAL_SECONDS
        ),
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
        lines: int = domain.DEFAULT_LOG_LINES,
        repo_root: str = "",
        *,
        pattern: str = "",
        context: int = domain.DEFAULT_LOG_CONTEXT,
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
        lines_clamped = lines > domain.MAX_LOG_LINES
        lines = min(lines, domain.MAX_LOG_LINES)
        context = min(context, domain.MAX_LOG_CONTEXT)

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
            selection = domain.select_log_lines(
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

        digest = domain.digest_log(results.read_log_lines(job_id))
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
        limit: int = domain.DEFAULT_JOB_LIST_LIMIT,
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
        limit_clamped = limit > domain.MAX_JOB_LIST_LIMIT
        limit = min(limit, domain.MAX_JOB_LIST_LIMIT)
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

    def summarize_scenario_results(
        self,
        job_ids: list[str] | None = None,
        feature_file: str = "",
        scenario_name: str = "",
        release: str = "",
        machine_type: str = "",
        status: str = "",
        limit: int = domain.DEFAULT_SUMMARIZE_FAILURES_LIMIT,
        repo_root: str = "",
    ) -> SummarizeScenarioResultsResponse:
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
        limit_clamped = limit > domain.MAX_SUMMARIZE_FAILURES_LIMIT
        limit = min(limit, domain.MAX_SUMMARIZE_FAILURES_LIMIT)

        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        results = self._results.bind(log_dir)
        in_memory_jobs = {job.job_id: job for job in self._registry.snapshot()}
        disk_job_ids = set(results.list_job_ids())

        job_counts = JobCounts()
        by_release: dict[str, dict[str, Any]] = {}
        by_machine_type: dict[str, dict[str, Any]] = {}
        failures: list[Failure] = []
        matched_job_ids: list[str] = []

        for job_id in sorted(set(in_memory_jobs) | disk_job_ids):
            job = in_memory_jobs.get(job_id)
            if job is None:
                job = self._recover_job(job_id, repo_root or None)
                if job is None:
                    continue

            metadata_record = results.read_record(job_id)
            if not domain.job_matches_result_filters(
                metadata_record,
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

            matched_job_ids.append(job_id)
            job_counts.total += 1
            if summary.status == RunStatus.RUNNING:
                job_counts.running += 1
            elif summary.status == RunStatus.COMPLETED:
                if summary.ok:
                    job_counts.completed_passed += 1
                else:
                    job_counts.completed_failed += 1
            else:
                job_counts.unknown += 1

            if summary.status != RunStatus.COMPLETED:
                continue

            report_data = results.read_report(job_id)
            if report_data is None:
                continue

            fallback_releases = metadata_record.releases
            fallback_machine_types = metadata_record.machine_types

            job_by_release, job_by_machine_type = (
                domain.grouped_counts_from_report(
                    report_data,
                    fallback_releases,
                    fallback_machine_types,
                )
            )
            domain.merge_grouped_counts(by_release, job_by_release)
            domain.merge_grouped_counts(by_machine_type, job_by_machine_type)
            failures.extend(
                domain.job_failures_from_report(
                    report_data,
                    job_id,
                    fallback_releases,
                    fallback_machine_types,
                )
            )

        truncated = len(failures) > limit

        return SummarizeScenarioResultsResponse(
            repo_root=str(resolved_repo_root),
            job_counts=job_counts,
            by_release=domain.grouped_counts_from_dict(by_release),
            by_machine_type=domain.grouped_counts_from_dict(by_machine_type),
            failures=failures[:limit],
            truncated=truncated,
            limit_clamped=limit_clamped,
            matched_job_ids=matched_job_ids,
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
                domain.summarize_report(report_data)
                if report_data is not None
                else None
            )
            report_ok = None if report is None else not report.failures

            pid = job.pid if job.pid is not None else record.pid
            pid_alive = False
            if handle is None and pid is not None:
                pid_alive = self._launcher.is_pid_alive(pid)

            classification = domain.classify_job_state(
                has_live_handle=handle is not None,
                returncode=returncode,
                report_present=report is not None,
                report_ok=report_ok,
                pid=pid,
                pid_alive=pid_alive,
            )
            status, ok = classification.status, classification.ok

        return JobSummary(
            job_id=job_id,
            status=status,
            ok=ok,
            returncode=returncode,
            feature_file=record.feature_file,
            scenario_name=record.scenario_name,
            machine_types=record.machine_types,
            releases=record.releases,
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
                    job_id, domain.DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=results.artifacts(job_id),
            )

        if handle is not None:
            handle.close()

        report_data = results.read_report(job_id)
        report = (
            domain.summarize_report(report_data)
            if report_data is not None
            else None
        )
        report_ok = None if report is None else not report.failures

        pid_alive = False
        if handle is None and job.pid is not None:
            pid_alive = self._launcher.is_pid_alive(job.pid)

        classification = domain.classify_job_state(
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
                    job_id, domain.DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=results.artifacts(job_id),
            )

        ok_value = bool(classification.ok)
        artifacts = results.artifacts(job_id)
        if report is None:
            response = CompletedResponse(
                ok=ok_value,
                job_id=job_id,
                returncode=returncode,
                artifacts=artifacts,
                summary=None,
                failures=[],
                recent_output=results.log_tail(
                    job_id, domain.DEFAULT_RUNNING_TAIL_LINES
                ),
            )
        else:
            response = CompletedResponse(
                ok=ok_value,
                job_id=job_id,
                returncode=returncode,
                artifacts=artifacts,
                summary=report.summary,
                failures=report.failures,
            )

        artifacts_dict = artifacts.model_dump(mode="json")
        record = results.read_record(job_id)
        record.job_id = job_id
        record.status = JobStatus.COMPLETED
        record.completed_at = self._now_utc()
        record.returncode = returncode
        record.ok = ok_value
        record.artifacts = artifacts
        results.write_record(job_id, record)
        results.append_event(
            {
                "event": "completed",
                "timestamp": self._now_utc(),
                "job_id": job_id,
                "ok": ok_value,
                "returncode": returncode,
                "artifacts": artifacts_dict,
            },
        )

        return response

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
