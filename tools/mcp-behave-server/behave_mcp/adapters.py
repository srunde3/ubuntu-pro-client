"""Concrete adapters implementing the ports in ``behave_mcp.ports``."""

import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from behave_mcp import domain
from behave_mcp.ports import (
    Job,
    LogFileOpenError,
    ProcessStartError,
    ReservationResult,
)


class SubprocessHandle:
    """Wraps a Popen process together with its owned stdout log file."""

    def __init__(self, process: Any, log_file: Any) -> None:
        self._process = process
        self._log_file = log_file

    def poll(self) -> int | None:
        return self._process.poll()

    def close(self) -> None:
        if self._log_file is not None and not self._log_file.closed:
            self._log_file.close()

    def terminate(self) -> None:
        self._process.terminate()


class SubprocessProcessLauncher:
    """Launches behave via ``subprocess.Popen`` writing to a log file."""

    def launch(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        stdout_log_path: Path,
    ) -> SubprocessHandle:
        try:
            log_file = stdout_log_path.open("w", encoding="utf-8")
        except OSError as exc:
            raise LogFileOpenError(str(exc)) from exc

        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as exc:
            log_file.close()
            raise ProcessStartError(str(exc)) from exc

        return SubprocessHandle(process, log_file)


class InMemoryJobRegistry:
    """Thread-safe in-memory registry of reserved and running behave jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def try_reserve(self, job: Job, max_parallel: int) -> ReservationResult:
        with self._lock:
            running_jobs = self._count_running_or_reserved_locked()
            if running_jobs >= max_parallel:
                return ReservationResult(
                    reserved=False,
                    running_jobs=running_jobs,
                    max_parallel=max_parallel,
                )
            self._jobs[job.job_id] = job
            return ReservationResult(
                reserved=True,
                running_jobs=running_jobs,
                max_parallel=max_parallel,
            )

    def register(self, job_id: str, job: Job) -> None:
        with self._lock:
            self._jobs[job_id] = job

    def release(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()

    def _count_running_or_reserved_locked(self) -> int:
        running_jobs = 0
        for job in self._jobs.values():
            if job.reserved:
                running_jobs += 1
                continue

            handle = job.process_handle
            if handle is None:
                continue

            if handle.poll() is None:
                running_jobs += 1

        return running_jobs


class LocalArtifactStore:
    """Filesystem-backed artifact persistence and log tailing."""

    def discover_feature_files(self, repo_root: Path) -> list[str]:
        features_dir = repo_root / "features"
        if not features_dir.exists():
            return []

        return sorted(
            str(path.relative_to(repo_root)).replace("\\", "/")
            for path in features_dir.rglob("*.feature")
        )

    def read_metadata(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_metadata(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            path.write_text(
                json.dumps(
                    payload, ensure_ascii=True, sort_keys=True, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            # Metadata write failures should not break job execution/status.
            return

    def append_index_event(self, log_dir: Path, event: dict[str, Any]) -> None:
        index_path = log_dir / domain._JOB_INDEX_FILE_NAME
        try:
            with index_path.open("a", encoding="utf-8") as index_stream:
                index_stream.write(
                    json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
                )
        except OSError:
            # Index write failures should not break job execution/status.
            return

    def tail_file(self, path: Path, lines: int) -> str:
        if not path.exists():
            return "Waiting for output..."

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            tail = deque(stream, maxlen=lines)
        return "".join(tail).rstrip() if tail else "Waiting for output..."

    def tail_lines(self, path: Path, lines: int) -> list[str]:
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            tail = deque(stream, maxlen=lines)
        return [line.rstrip("\n") for line in tail]

    def read_report_json(self, path: Path) -> list[Any] | None:
        if not path.exists():
            return None

        try:
            report_data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(report_data, list):
            return None

        return report_data

    def exists(self, path: Path) -> bool:
        return path.exists()


class EnvConfig:
    """Reads configuration from the process environment on each call."""

    def resolve_repo_root(self, override: str | None) -> Path:
        if override:
            return self._validated_repo_root(Path(override).expanduser())

        env_value = os.environ.get("UBUNTU_PRO_CLIENT_REPO")
        if env_value:
            return self._validated_repo_root(Path(env_value).expanduser())

        current = Path(__file__).resolve()
        for candidate in [current, *current.parents]:
            if (candidate / "features").exists() and (
                candidate / "tox.ini"
            ).exists():
                return candidate.resolve()

        return self._validated_repo_root(current.parents[3])

    def resolve_log_dir(self, repo_root: Path) -> Path:
        env_path = os.environ.get("MCP_LOG_DIR")
        if env_path:
            log_dir = Path(env_path).resolve()
        else:
            log_dir = repo_root / ".mcp_behave_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def allow_cloud_machine_types(self) -> bool:
        return self._env_flag_enabled(domain.ALLOW_CLOUD_MACHINE_TYPES_ENV_VAR)

    def max_parallel_jobs(self) -> tuple[int | None, str | None]:
        value = os.environ.get(domain.MAX_PARALLEL_JOBS_ENV_VAR, "").strip()
        if not value:
            return domain._DEFAULT_MAX_PARALLEL_JOBS, None

        try:
            parsed_value = int(value)
        except ValueError:
            return (
                None,
                f"{domain.MAX_PARALLEL_JOBS_ENV_VAR} must be a "
                "positive integer",
            )

        if parsed_value <= 0:
            return (
                None,
                f"{domain.MAX_PARALLEL_JOBS_ENV_VAR} must be a "
                "positive integer",
            )

        return parsed_value, None

    def subprocess_env(self) -> dict[str, str]:
        return os.environ.copy()

    def transport(self) -> str:
        return os.environ.get("MCP_TRANSPORT", "stdio")

    def _validated_repo_root(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        features_dir = resolved / "features"
        tox_file = resolved / "tox.ini"
        if not features_dir.exists() or not tox_file.exists():
            raise ValueError(
                "Invalid repo_root: expected directory containing "
                "features/ and tox.ini"
            )
        return resolved

    @staticmethod
    def _env_flag_enabled(name: str) -> bool:
        value = os.environ.get(name, "")
        return value.strip().lower() in {"1", "true", "yes", "on"}
