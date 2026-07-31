"""Application service orchestrating behave jobs via injected ports."""

from pathlib import Path
from typing import Callable

from behave_mcp import domain
from behave_mcp.config import Settings
from behave_mcp.messages import (
    ArtifactsResponse,
    Capacity,
    CapacityExceededResponse,
    CompletedResponse,
    DescribeFeatureResponse,
    ExistsFlags,
    FeatureDetail,
    FindScenariosResponse,
    ListDimensionsResponse,
    ListFeaturesResponse,
    LogsResponse,
    RunningResponse,
    ScenarioMatch,
    StartScenarioResponse,
    StartScenarioResult,
    TimeoutResponse,
    WaitForCompletionResult,
)
from behave_mcp.ports import (
    ArtifactStore,
    FeatureFileReader,
    Job,
    JobRegistry,
    LogFileOpenError,
    ProcessLauncher,
    ProcessStartError,
    Workspace,
)


class BehaveServiceError(Exception):
    """Raised for expected, user-facing failures (bad input, unknown job, etc.).

    FastMCP catches any exception raised from a tool function and reports it
    to the MCP client as ``isError: true`` with this message as the text
    content.
    """


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
            domain.catalog_entry(detail)
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

        normalized = domain.normalize_feature_file_arg(feature_file)
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
        dimensions = domain.aggregate_dimensions(details)
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
                if not domain.scenario_matches(
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
                        combos=domain.filtered_combos(
                            scenario, release, machine_type
                        ),
                    )
                )

        return FindScenariosResponse(
            repo_root=str(resolved_repo_root),
            matches=matches,
        )

    @staticmethod
    def _feature_has_match(
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
            domain.scenario_matches(
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

        normalized = domain.normalize_feature_file_arg(feature_file)
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
        max_wait_seconds: int = domain._DEFAULT_WAIT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = (
            domain._DEFAULT_WAIT_POLL_INTERVAL_SECONDS
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
        lines: int = domain._DEFAULT_LOG_TAIL_LINES,
        repo_root: str = "",
    ) -> LogsResponse:
        lines = max(1, min(lines, domain._MAX_LOG_TAIL_LINES))

        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise BehaveServiceError(f"Unknown job_id: {job_id}")

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
                raise BehaveServiceError(f"Unknown job_id: {job_id}")

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

    def _status_payload(
        self, job_id: str, repo_root_override: str | None
    ) -> RunningResponse | CompletedResponse:
        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root_override)
            except ValueError as exc:
                raise BehaveServiceError(str(exc)) from exc
            if job is None:
                raise BehaveServiceError(f"Unknown job_id: {job_id}")

        handle = job.process_handle
        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata = job.metadata
        log_dir = stdout_log.parent

        if handle is not None:
            returncode = handle.poll()
            if returncode is None:
                return RunningResponse(
                    job_id=job_id,
                    recent_output=self._artifact_store.tail_file(
                        stdout_log, domain._DEFAULT_RUNNING_TAIL_LINES
                    ),
                    artifacts=domain.artifacts_payload(
                        log_dir=log_dir,
                        stdout_log=stdout_log,
                        json_report=json_report,
                        metadata=metadata,
                    ),
                )
        else:
            returncode = None

        if handle is not None:
            handle.close()

        report_data = self._artifact_store.read_report_json(json_report)
        report = (
            domain.summarize_report(report_data)
            if report_data is not None
            else None
        )
        ok_value = returncode == 0 if returncode is not None else False
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
                    stdout_log, domain._DEFAULT_RUNNING_TAIL_LINES
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
            return None

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

        return Job(
            job_id=job_id,
            process_handle=None,
            stdout_log=stdout_log,
            json_report=json_report,
            metadata=metadata,
            reserved=False,
        )
