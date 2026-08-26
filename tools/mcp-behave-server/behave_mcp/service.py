"""Application service orchestrating behave jobs via injected ports."""

import logging
from pathlib import Path
from typing import Any, Callable

from behave_mcp import domain
from behave_mcp.config import Settings
from behave_mcp.messages import (
    ArtifactsResponse,
    Capacity,
    CapacityExceededResponse,
    CompletedResponse,
    DescribeFeatureResponse,
    ExistsFlags,
    Failure,
    FeatureDetail,
    FindScenariosResponse,
    JobCounts,
    JobSummary,
    ListDimensionsResponse,
    ListFeaturesResponse,
    ListScenarioJobsResponse,
    LogsResponse,
    RunningResponse,
    ScenarioMatch,
    StartScenarioResponse,
    StartScenarioResult,
    SummarizeScenarioResultsResponse,
    TimeoutResponse,
    WaitForCompletionResult,
)
from behave_mcp.ports import (
    ArtifactStore,
    FeatureCatalog,
    FeatureFileReader,
    Job,
    JobRegistry,
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
        feature_catalog: FeatureCatalog,
        artifact_store: ArtifactStore,
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
        self._feature_catalog = feature_catalog
        self._artifact_store = artifact_store
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
            self._feature_catalog.catalog_entry(detail)
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

        normalized = self._feature_catalog.normalize_feature_file_arg(
            feature_file
        )
        details = self._feature_reader.discover_feature_details(
            resolved_repo_root
        )
        for detail in details:
            if detail.path == normalized:
                return DescribeFeatureResponse(
                    feature_file=detail.path,
                    title=detail.title,
                    tags=detail.tags,
                    requires_config=detail.requires_config,
                    scenarios=detail.scenarios,
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
        dimensions = self._feature_catalog.aggregate_dimensions(details)
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
                if not self._feature_catalog.scenario_matches(
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
                        requires_config=scenario.requires_config,
                        combos=self._feature_catalog.filtered_combos(
                            scenario, release, machine_type
                        ),
                    )
                )

        return FindScenariosResponse(
            repo_root=str(resolved_repo_root),
            matches=matches,
        )

    def _feature_has_match(
        self,
        feature_detail: FeatureDetail,
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
            self._feature_catalog.scenario_matches(
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
    ) -> StartScenarioResult:
        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        normalized = self._feature_catalog.normalize_feature_file_arg(
            feature_file
        )
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

        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        job_id = self._new_job_id()
        json_report_path = log_dir / f"{job_id}_report.json"
        stdout_path = log_dir / f"{job_id}_stdout.log"
        metadata_path = log_dir / f"{job_id}_meta.json"

        reserved_job = Job(
            job_id=job_id,
            process_handle=None,
            stdout_log=stdout_path,
            json_report=json_report_path,
            metadata=metadata_path,
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
            json_report_path,
        )
        env = self._workspace.subprocess_env()

        try:
            handle = self._launcher.launch(
                command, str(resolved_repo_root), env, stdout_path
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
                stdout_log=stdout_path,
                json_report=json_report_path,
                metadata=metadata_path,
                reserved=False,
                pid=handle.pid,
            ),
        )

        artifacts = domain.artifacts_payload(
            log_dir=log_dir,
            stdout_log=stdout_path,
            json_report=json_report_path,
            metadata=metadata_path,
        )
        artifacts_dict = artifacts.model_dump(mode="json")
        self._artifact_store.write_metadata(
            metadata_path,
            {
                "job_id": job_id,
                "status": "started",
                "started_at": self._now_utc(),
                "feature_file": feature_file,
                "scenario_name": scenario_name,
                "machine_types": machine_types,
                "releases": releases or [],
                "command": command,
                "repo_root": str(resolved_repo_root),
                "pid": handle.pid,
                "artifacts": artifacts_dict,
            },
        )
        self._artifact_store.append_index_event(
            log_dir,
            {
                "event": "started",
                "timestamp": self._now_utc(),
                "job_id": job_id,
                "feature_file": feature_file,
                "scenario_name": scenario_name,
                "machine_types": machine_types,
                "releases": releases or [],
                "artifacts": artifacts_dict,
            },
        )

        return StartScenarioResponse(
            job_id=job_id,
            message="Test started. Call wait_for_scenario_completion.",
            artifacts=artifacts,
        )

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
                    last_status="running",
                    recent_output=payload.recent_output,
                    artifacts=payload.artifacts,
                )

            self._sleep(poll_interval_seconds)

    def get_logs(
        self,
        job_id: str,
        lines: int = domain.DEFAULT_LOG_TAIL_LINES,
        repo_root: str = "",
    ) -> LogsResponse:
        lines = max(1, min(lines, domain.MAX_LOG_TAIL_LINES))

        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise UnknownJobError(job_id)

        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata = job.metadata
        log_dir = stdout_log.parent
        if not self._artifact_store.exists(stdout_log):
            raise BehaveServiceError(
                f"No log file exists for job_id: {job_id}"
            )

        return LogsResponse(
            job_id=job_id,
            lines=lines,
            output=self._artifact_store.tail_file(stdout_log, lines),
            output_lines=self._artifact_store.tail_lines(stdout_log, lines),
            artifacts=domain.artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata,
            ),
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

        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata = job.metadata
        log_dir = stdout_log.parent

        return ArtifactsResponse(
            job_id=job_id,
            artifacts=domain.artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata,
            ),
            metadata=self._artifact_store.read_metadata(metadata),
            exists=ExistsFlags(
                stdout_log=self._artifact_store.exists(stdout_log),
                json_report=self._artifact_store.exists(json_report),
                metadata=self._artifact_store.exists(metadata),
            ),
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

        limit = max(1, min(limit, domain.MAX_JOB_LIST_LIMIT))
        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)

        in_memory_jobs = {job.job_id: job for job in self._registry.snapshot()}
        disk_job_ids = set(self._artifact_store.list_job_ids(log_dir))
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
        running = [s for s in summaries if s.status == "running"]
        others = [s for s in summaries if s.status != "running"]
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
        if status and status not in ("running", "completed", "unknown"):
            raise BehaveServiceError(
                "Invalid status filter: "
                f"{status}. Allowed values: running, completed, unknown"
            )

        try:
            resolved_repo_root = self._workspace.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            raise BehaveServiceError(str(exc)) from exc

        normalized_feature_file = (
            self._feature_catalog.normalize_feature_file_arg(feature_file)
            if feature_file
            else None
        )
        job_ids_filter = set(job_ids) if job_ids else None
        limit = max(1, min(limit, domain.MAX_SUMMARIZE_FAILURES_LIMIT))

        log_dir = self._workspace.resolve_log_dir(resolved_repo_root)
        in_memory_jobs = {job.job_id: job for job in self._registry.snapshot()}
        disk_job_ids = set(self._artifact_store.list_job_ids(log_dir))

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

            metadata_payload = self._artifact_store.read_metadata(job.metadata)
            if not domain.job_matches_result_filters(
                metadata_payload,
                job_id=job_id,
                job_ids=job_ids_filter,
                feature_file=normalized_feature_file,
                scenario_name=scenario_name or None,
                release=release or None,
                machine_type=machine_type or None,
            ):
                continue

            summary = self._job_summary(job_id, job)
            if status and summary.status != status:
                continue

            matched_job_ids.append(job_id)
            job_counts.total += 1
            if summary.status == "running":
                job_counts.running += 1
            elif summary.status == "completed":
                if summary.ok:
                    job_counts.completed_passed += 1
                else:
                    job_counts.completed_failed += 1
            else:
                job_counts.unknown += 1

            if summary.status != "completed":
                continue

            report_data = self._artifact_store.read_report_json(
                job.json_report
            )
            if report_data is None:
                continue

            fallback_releases = metadata_payload.get("releases") or []
            fallback_machine_types = (
                metadata_payload.get("machine_types") or []
            )

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
            matched_job_ids=matched_job_ids,
        )

    def _job_summary(self, job_id: str, job: Job) -> JobSummary:
        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata_path = job.metadata
        log_dir = stdout_log.parent
        metadata_payload = self._artifact_store.read_metadata(metadata_path)

        if job.reserved:
            status, ok, returncode = "running", None, None
        else:
            handle = job.process_handle
            returncode = handle.poll() if handle is not None else None
            report_data = None
            if handle is None or returncode is not None:
                report_data = self._artifact_store.read_report_json(
                    json_report
                )
            report = (
                domain.summarize_report(report_data)
                if report_data is not None
                else None
            )
            report_ok = None if report is None else not report.failures

            pid = (
                job.pid if job.pid is not None else metadata_payload.get("pid")
            )
            pid_alive = False
            if handle is None and pid is not None:
                pid_alive = self._launcher.is_pid_alive(pid)

            classification = domain.classify_job_status(
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
            feature_file=metadata_payload.get("feature_file", ""),
            scenario_name=metadata_payload.get("scenario_name", ""),
            machine_types=metadata_payload.get("machine_types", []),
            releases=metadata_payload.get("releases", []),
            started_at=metadata_payload.get("started_at"),
            completed_at=metadata_payload.get("completed_at"),
            artifacts=domain.artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata_path,
            ),
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
        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata = job.metadata
        log_dir = stdout_log.parent

        returncode = handle.poll() if handle is not None else None
        if handle is not None and returncode is None:
            return RunningResponse(
                job_id=job_id,
                recent_output=self._artifact_store.tail_file(
                    stdout_log, domain.DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=domain.artifacts_payload(
                    log_dir=log_dir,
                    stdout_log=stdout_log,
                    json_report=json_report,
                    metadata=metadata,
                ),
            )

        if handle is not None:
            handle.close()

        report_data = self._artifact_store.read_report_json(json_report)
        report = (
            domain.summarize_report(report_data)
            if report_data is not None
            else None
        )
        report_ok = None if report is None else not report.failures

        pid_alive = False
        if handle is None and job.pid is not None:
            pid_alive = self._launcher.is_pid_alive(job.pid)

        classification = domain.classify_job_status(
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

        if classification.status == "running":
            return RunningResponse(
                job_id=job_id,
                recent_output=self._artifact_store.tail_file(
                    stdout_log, domain.DEFAULT_RUNNING_TAIL_LINES
                ),
                artifacts=domain.artifacts_payload(
                    log_dir=log_dir,
                    stdout_log=stdout_log,
                    json_report=json_report,
                    metadata=metadata,
                ),
            )

        ok_value = bool(classification.ok)
        artifacts = domain.artifacts_payload(
            log_dir=log_dir,
            stdout_log=stdout_log,
            json_report=json_report,
            metadata=metadata,
        )
        if report is None:
            response = CompletedResponse(
                ok=ok_value,
                job_id=job_id,
                returncode=returncode,
                artifacts=artifacts,
                summary=None,
                failures=[],
                recent_output=self._artifact_store.tail_file(
                    stdout_log, domain.DEFAULT_RUNNING_TAIL_LINES
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
        existing_metadata = self._artifact_store.read_metadata(metadata)
        existing_metadata.update(
            {
                "job_id": job_id,
                "status": "completed",
                "completed_at": self._now_utc(),
                "returncode": returncode,
                "ok": ok_value,
                "artifacts": artifacts_dict,
            }
        )
        self._artifact_store.write_metadata(metadata, existing_metadata)
        self._artifact_store.append_index_event(
            log_dir,
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
        stdout_log = log_dir / f"{job_id}_stdout.log"
        json_report = log_dir / f"{job_id}_report.json"
        metadata = log_dir / f"{job_id}_meta.json"
        if (
            not self._artifact_store.exists(stdout_log)
            and not self._artifact_store.exists(json_report)
            and not self._artifact_store.exists(metadata)
        ):
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
        metadata_payload = self._artifact_store.read_metadata(metadata)
        if metadata_payload:
            metadata_artifacts = metadata_payload.get("artifacts", {})
            stdout_log = Path(
                metadata_artifacts.get("stdout_log", str(stdout_log))
            )
            json_report = Path(
                metadata_artifacts.get("json_report", str(json_report))
            )
            metadata = Path(metadata_artifacts.get("metadata", str(metadata)))

        job = Job(
            job_id=job_id,
            process_handle=None,
            stdout_log=stdout_log,
            json_report=json_report,
            metadata=metadata,
            reserved=False,
            pid=metadata_payload.get("pid"),
        )
        # Cache the recovery so later calls in this process hit the
        # registry instead of re-reading disk and re-logging every poll.
        self._registry.register(job_id, job)
        return job
