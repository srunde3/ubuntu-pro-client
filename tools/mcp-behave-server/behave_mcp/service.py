"""Application service orchestrating behave jobs via injected ports."""

from pathlib import Path
from typing import Any, Callable

from behave_mcp import domain
from behave_mcp.ports import (
    ArtifactStore,
    Config,
    Job,
    JobRegistry,
    LogFileOpenError,
    ProcessLauncher,
    ProcessStartError,
)


class BehaveService:
    """Coordinates feature discovery and behave job lifecycle.

    All external interactions are delegated to injected ports and callables,
    so tests can supply fakes.
    """

    def __init__(
        self,
        *,
        config: Config,
        artifact_store: ArtifactStore,
        registry: JobRegistry,
        launcher: ProcessLauncher,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        now_utc: Callable[[], str],
        new_job_id: Callable[[], str],
    ) -> None:
        self._config = config
        self._artifact_store = artifact_store
        self._registry = registry
        self._launcher = launcher
        self._monotonic = monotonic
        self._sleep = sleep
        self._now_utc = now_utc
        self._new_job_id = new_job_id

    def list_features(self, repo_root: str = "") -> dict[str, Any]:
        try:
            resolved_repo_root = self._config.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "features": []}

        return {
            "ok": True,
            "repo_root": str(resolved_repo_root),
            "features": self._artifact_store.discover_feature_files(
                resolved_repo_root
            ),
        }

    def start_scenario(
        self,
        feature_file: str,
        machine_types: list[str],
        scenario_name: str = "",
        releases: list[str] | None = None,
        repo_root: str = "",
    ) -> dict[str, Any]:
        try:
            resolved_repo_root = self._config.resolve_repo_root(
                repo_root or None
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        normalized = domain.normalize_feature_file_arg(feature_file)
        allowed_features = set(
            self._artifact_store.discover_feature_files(resolved_repo_root)
        )
        if normalized not in allowed_features:
            return {
                "ok": False,
                "error": (
                    "Feature is not listed by list_features: "
                    f"{feature_file}"
                ),
            }

        machine_type_error = domain.validate_machine_types(
            machine_types, self._config.allow_cloud_machine_types()
        )
        if machine_type_error:
            return {"ok": False, "error": machine_type_error}

        log_dir = self._config.resolve_log_dir(resolved_repo_root)
        job_id = self._new_job_id()
        json_report_path = log_dir / f"{job_id}_report.json"
        stdout_path = log_dir / f"{job_id}_stdout.log"
        metadata_path = log_dir / f"{job_id}_meta.json"

        max_parallel_jobs, parse_error = self._config.max_parallel_jobs()
        if parse_error:
            return {"ok": False, "error": parse_error}

        reserved_job = Job(
            job_id=job_id,
            process_handle=None,
            stdout_log=stdout_path,
            json_report=json_report_path,
            metadata=metadata_path,
            reserved=True,
        )
        reservation = self._registry.try_reserve(
            reserved_job, max_parallel_jobs
        )
        if not reservation.reserved:
            return {
                "ok": False,
                "status": "capacity_exceeded",
                "error": (
                    "Maximum parallel behave jobs reached. "
                    f"Set {domain.MAX_PARALLEL_JOBS_ENV_VAR} to a "
                    "higher value or wait for an active job to complete."
                ),
                "capacity": {
                    "max_parallel_jobs": reservation.max_parallel,
                    "running_jobs": reservation.running_jobs,
                },
            }

        command = domain.build_command(
            feature_file,
            machine_types,
            scenario_name,
            releases,
            json_report_path,
        )
        env = self._config.subprocess_env()

        try:
            handle = self._launcher.launch(
                command, str(resolved_repo_root), env, stdout_path
            )
        except LogFileOpenError as exc:
            self._registry.release(job_id)
            return {
                "ok": False,
                "error": f"Failed to open log file for job_id {job_id}: {exc}",
            }
        except ProcessStartError as exc:
            self._registry.release(job_id)
            return {
                "ok": False,
                "error": f"Failed to start behave scenario: {exc}",
            }

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
                "artifacts": artifacts,
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
                "artifacts": artifacts,
            },
        )

        return {
            "ok": True,
            "job_id": job_id,
            "status": "started",
            "message": "Test started. Call wait_for_scenario_completion.",
            "artifacts": artifacts,
        }

    def wait_for_completion(
        self,
        job_id: str,
        max_wait_seconds: int = domain._DEFAULT_WAIT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = (
            domain._DEFAULT_WAIT_POLL_INTERVAL_SECONDS
        ),
        repo_root: str = "",
    ) -> dict[str, Any]:
        if max_wait_seconds <= 0:
            return {
                "ok": False,
                "error": "max_wait_seconds must be greater than 0",
            }
        if poll_interval_seconds <= 0:
            return {
                "ok": False,
                "error": "poll_interval_seconds must be greater than 0",
            }

        deadline = self._monotonic() + max_wait_seconds

        while True:
            payload = self._status_payload(job_id, repo_root or None)
            if payload.get("ok") is False:
                return payload

            if payload.get("status") == "completed":
                return payload

            if self._monotonic() >= deadline:
                return {
                    "ok": False,
                    "status": "timeout",
                    "job_id": job_id,
                    "max_wait_seconds": max_wait_seconds,
                    "poll_interval_seconds": poll_interval_seconds,
                    "last_status": "running",
                    "recent_output": payload.get("recent_output", ""),
                    "artifacts": payload.get("artifacts"),
                }

            self._sleep(poll_interval_seconds)

    def get_logs(
        self,
        job_id: str,
        lines: int = domain._DEFAULT_LOG_TAIL_LINES,
        repo_root: str = "",
    ) -> dict[str, Any]:
        lines = max(1, min(lines, domain._MAX_LOG_TAIL_LINES))

        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if job is None:
                return {"ok": False, "error": f"Unknown job_id: {job_id}"}

        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata = job.metadata
        log_dir = stdout_log.parent
        if not self._artifact_store.exists(stdout_log):
            return {
                "ok": False,
                "error": f"No log file exists for job_id: {job_id}",
            }

        return {
            "ok": True,
            "job_id": job_id,
            "lines": lines,
            "output": self._artifact_store.tail_file(stdout_log, lines),
            "output_lines": self._artifact_store.tail_lines(stdout_log, lines),
            "artifacts": domain.artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata,
            ),
        }

    def get_artifacts(
        self, job_id: str, repo_root: str = ""
    ) -> dict[str, Any]:
        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root or None)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if job is None:
                return {"ok": False, "error": f"Unknown job_id: {job_id}"}

        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata = job.metadata
        log_dir = stdout_log.parent

        return {
            "ok": True,
            "job_id": job_id,
            "artifacts": domain.artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata,
            ),
            "metadata": self._artifact_store.read_metadata(metadata),
            "exists": {
                "stdout_log": self._artifact_store.exists(stdout_log),
                "json_report": self._artifact_store.exists(json_report),
                "metadata": self._artifact_store.exists(metadata),
            },
        }

    def _status_payload(
        self, job_id: str, repo_root_override: str | None
    ) -> dict[str, Any]:
        job = self._registry.get(job_id)
        if job is None:
            try:
                job = self._recover_job(job_id, repo_root_override)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if job is None:
                return {"ok": False, "error": f"Unknown job_id: {job_id}"}

        handle = job.process_handle
        stdout_log = job.stdout_log
        json_report = job.json_report
        metadata = job.metadata
        log_dir = stdout_log.parent

        if handle is not None:
            returncode = handle.poll()
            if returncode is None:
                return {
                    "ok": True,
                    "status": "running",
                    "job_id": job_id,
                    "recent_output": self._artifact_store.tail_file(
                        stdout_log, domain._DEFAULT_RUNNING_TAIL_LINES
                    ),
                    "artifacts": domain.artifacts_payload(
                        log_dir=log_dir,
                        stdout_log=stdout_log,
                        json_report=json_report,
                        metadata=metadata,
                    ),
                }
        else:
            returncode = None

        if handle is not None:
            handle.close()

        report_data = self._artifact_store.read_report_json(json_report)
        summary_payload = (
            domain.summarize_report(report_data)
            if report_data is not None
            else None
        )
        response: dict[str, Any] = {
            "ok": returncode == 0 if returncode is not None else False,
            "status": "completed",
            "job_id": job_id,
            "returncode": returncode,
            "artifacts": domain.artifacts_payload(
                log_dir=log_dir,
                stdout_log=stdout_log,
                json_report=json_report,
                metadata=metadata,
            ),
        }
        if summary_payload is None:
            response["summary"] = None
            response["failures"] = []
            response["recent_output"] = self._artifact_store.tail_file(
                stdout_log, domain._DEFAULT_RUNNING_TAIL_LINES
            )
        else:
            response.update(summary_payload)

        existing_metadata = self._artifact_store.read_metadata(metadata)
        existing_metadata.update(
            {
                "job_id": job_id,
                "status": "completed",
                "completed_at": self._now_utc(),
                "returncode": returncode,
                "ok": response["ok"],
                "artifacts": response["artifacts"],
            }
        )
        self._artifact_store.write_metadata(metadata, existing_metadata)
        self._artifact_store.append_index_event(
            log_dir,
            {
                "event": "completed",
                "timestamp": self._now_utc(),
                "job_id": job_id,
                "ok": response["ok"],
                "returncode": returncode,
                "artifacts": response["artifacts"],
            },
        )

        return response

    def _recover_job(
        self, job_id: str, repo_root_override: str | None
    ) -> Job | None:
        resolved_repo_root = self._config.resolve_repo_root(repo_root_override)
        log_dir = self._config.resolve_log_dir(resolved_repo_root)
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
