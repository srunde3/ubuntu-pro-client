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
    job_matches_result_filters,
    job_state,
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
    InterruptedResponse,
    JobExit,
    JobRecord,
    JobResult,
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
    JobLauncher,
    JobResultStore,
    JobResultStoreFactory,
    JobStartError,
    LanePool,
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
    """Raised when no job with this id was ever launched."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"Unknown job_id: {job_id}. Call list_scenario_jobs to see "
            "known jobs."
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
        lanes: LanePool,
        launcher: JobLauncher,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        now_utc: Callable[[], str],
        new_job_id: Callable[[], str],
    ) -> None:
        self._workspace = workspace
        self._settings = settings
        self._feature_reader = feature_reader
        self._results = results
        self._lanes = lanes
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

        results = self._results.bind(self._log_dir())
        pool = POOL_OF[machine_type]
        limit = self._settings.pool_limits.get(pool, 0)
        lease = self._lanes.take(pool, limit)
        if lease is None:
            return CapacityExceededResponse(
                error=(
                    f"No room in the {pool} pool. Wait for an active job "
                    f"to complete, or raise {POOL_LIMIT_ENV_VARS[pool]}."
                ),
                full_pools=[
                    PoolCapacity(
                        pool=pool.value,
                        limit=limit,
                        running=self._lanes.busy(pool, limit),
                    )
                ],
            )

        job_id = self._new_job_id()
        write_targets = results.write_targets(job_id)
        command = build_command(
            feature_file,
            scenario_name,
            release,
            machine_type,
            write_targets.json_report,
        )
        env = self._workspace.subprocess_env()
        env[INSTALL_FROM_ENV_VAR] = install_from

        artifacts = results.artifacts(job_id)
        # Written before the wrapper exists, so a launch that dies part way
        # reads as an interrupted job rather than as no job at all.
        results.write_record(
            job_id,
            JobRecord(
                job_id=job_id,
                started_at=self._now_utc(),
                feature_file=feature_file,
                scenario_name=scenario_name,
                release=release,
                machine_type=machine_type,
                install_from=install_from,
                command=command,
                repo_root=str(resolved_repo_root),
                repo_state=repo_state,
                artifacts=artifacts,
            ),
        )
        try:
            self._launcher.launch(
                job_id, command, str(resolved_repo_root), env, lease
            )
        except JobStartError as exc:
            raise BehaveServiceError(
                f"Failed to start behave scenario: {exc}"
            ) from exc

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

    def kill_job(self, job_id: str) -> KillJobResponse:
        """Stop a running job, from any process.

        The job's wrapper passes the signal to tox and waits for it, so the
        job ends and records its exit as killed. A campaign's next tick then
        records the unit, rather than waiting on it forever.
        """
        results = self._known(job_id)
        state = self._state(job_id, results)
        if state == RunStatus.COMPLETED:
            message = "Job had already finished."
        elif state == RunStatus.INTERRUPTED:
            message = "Job is not running: its wrapper is gone."
        elif self._launcher.signal(job_id):
            return KillJobResponse(
                job_id=job_id, killed=True, message="Termination signalled."
            )
        else:
            message = "Job's wrapper could not be signalled."
        return KillJobResponse(job_id=job_id, killed=False, message=message)

    def job_status(
        self, job_id: str
    ) -> RunningResponse | CompletedResponse | InterruptedResponse:
        """Return a job's current state without waiting.

        ``wait_for_completion`` requires a positive timeout, so this is what
        a caller polls when it must not block -- a scheduler filling lanes,
        for instance.
        """
        return self._status_payload(job_id)

    def wait_for_completion(
        self,
        job_id: str,
        max_wait_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = (DEFAULT_WAIT_POLL_INTERVAL_SECONDS),
    ) -> WaitForCompletionResult:
        if max_wait_seconds <= 0:
            raise BehaveServiceError("max_wait_seconds must be greater than 0")
        if poll_interval_seconds <= 0:
            raise BehaveServiceError(
                "poll_interval_seconds must be greater than 0"
            )

        deadline = self._monotonic() + max_wait_seconds

        while True:
            payload = self._status_payload(job_id)

            if not isinstance(payload, RunningResponse):
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

        results = self._known(job_id)
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

    def get_errors(self, job_id: str) -> ErrorsResponse:
        results = self._known(job_id)
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

    def get_artifacts(self, job_id: str) -> ArtifactsResponse:
        results = self._known(job_id)
        return ArtifactsResponse(
            job_id=job_id,
            artifacts=results.artifacts(job_id),
            record=results.read_record(job_id),
            exit=results.read_exit(job_id),
            exists=results.exists(job_id),
        )

    def list_jobs(
        self, limit: int = DEFAULT_JOB_LIST_LIMIT
    ) -> ListScenarioJobsResponse:
        if limit <= 0:
            raise BehaveServiceError(
                f"limit must be a positive integer, got {limit}"
            )
        limit_clamped = limit > MAX_JOB_LIST_LIMIT
        limit = min(limit, MAX_JOB_LIST_LIMIT)
        results = self._results.bind(self._log_dir())

        summaries = [
            self._job_summary(job_id, results)
            for job_id in results.list_job_ids()
        ]
        summaries.sort(key=lambda summary: summary.started_at or "")
        running = [s for s in summaries if s.status == RunStatus.RUNNING]
        others = [s for s in summaries if s.status != RunStatus.RUNNING]
        trimmed = running + others[-limit:]

        return ListScenarioJobsResponse(
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

        results = self._results.bind(self._log_dir())

        matched: list[tuple[str, JobSummary]] = []
        for job_id in results.list_job_ids():
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
            summary = self._job_summary(job_id, results)
            if status_filter is not None and summary.status != status_filter:
                continue
            matched.append((job_id, summary))

        matched.sort(key=lambda item: item[1].started_at or "", reverse=True)
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
        self, job_id: str, summary: JobSummary, results: JobResultStore
    ) -> JobResult:
        report = None
        if summary.status == RunStatus.COMPLETED:
            report_data = results.read_report(job_id)
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

    def _job_summary(self, job_id: str, results: JobResultStore) -> JobSummary:
        record = results.read_record(job_id)
        exit = results.read_exit(job_id)
        status = self._state(job_id, results, exit)
        return JobSummary(
            job_id=job_id,
            status=status,
            ok=(
                exit.ok
                if exit is not None
                else (None if status == RunStatus.RUNNING else False)
            ),
            returncode=exit.returncode if exit is not None else None,
            feature_file=record.feature_file,
            scenario_name=record.scenario_name,
            release=record.release,
            machine_type=record.machine_type,
            started_at=record.started_at,
            completed_at=exit.completed_at if exit is not None else None,
            artifacts=results.artifacts(job_id),
        )

    def _status_payload(
        self, job_id: str
    ) -> RunningResponse | CompletedResponse | InterruptedResponse:
        results = self._known(job_id)
        exit = results.read_exit(job_id)
        state = self._state(job_id, results, exit)
        artifacts = results.artifacts(job_id)

        if state == RunStatus.RUNNING:
            return RunningResponse(
                job_id=job_id,
                recent_output=results.log_tail(
                    job_id, DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=artifacts,
            )

        record = results.read_record(job_id)
        if exit is None:
            return InterruptedResponse(
                job_id=job_id,
                artifacts=artifacts,
                recent_output=results.log_tail(
                    job_id, DEFAULT_RUNNING_TAIL_LINES
                ),
                feature_file=record.feature_file,
                scenario_name=record.scenario_name,
                release=record.release,
                machine_type=record.machine_type,
            )

        report_data = results.read_report(job_id)
        report = (
            summarize_report(report_data) if report_data is not None else None
        )
        return CompletedResponse(
            ok=exit.ok,
            job_id=job_id,
            returncode=exit.returncode,
            artifacts=artifacts,
            summary=None if report is None else report.summary,
            failures=[] if report is None else report.failures,
            recent_output=(
                results.log_tail(job_id, DEFAULT_RUNNING_TAIL_LINES)
                if report is None
                else None
            ),
            feature_file=record.feature_file,
            scenario_name=record.scenario_name,
            release=record.release,
            machine_type=record.machine_type,
        )

    def _known(self, job_id: str) -> JobResultStore:
        """The store holding ``job_id``, which must have been launched."""
        results = self._results.bind(self._log_dir())
        if not results.exists(job_id).record:
            raise UnknownJobError(job_id)
        return results

    def _state(
        self,
        job_id: str,
        results: JobResultStore,
        exit: JobExit | None = None,
    ) -> RunStatus:
        if exit is None:
            exit = results.read_exit(job_id)
        return job_state(
            exited=exit is not None,
            lock_held=exit is None and self._launcher.is_running(job_id),
        )

    def _log_dir(self) -> Path:
        try:
            return self._workspace.resolve_log_dir()
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc
