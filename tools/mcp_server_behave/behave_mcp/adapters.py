"""Concrete adapters implementing the ports in ``behave_mcp.ports``."""

import json
import logging
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from behave_mcp import domain, parser
from behave_mcp.messages import Artifacts, ExistsFlags
from behave_mcp.ports import (
    Job,
    LogFileOpenError,
    ProcessStartError,
    ReservationResult,
    WriteTargets,
)

logger = logging.getLogger(__name__)


class SubprocessHandle:
    """Wraps a Popen process together with its owned stdout log file."""

    def __init__(self, process: Any, log_file: Any) -> None:
        self._process = process
        self._log_file = log_file

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def close(self) -> None:
        if self._log_file is not None and not self._log_file.closed:
            self._log_file.close()

    def terminate(self) -> None:
        self._process.terminate()


class PopenLauncher:
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

    def is_pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but is owned by another user/uid.
            return True
        except OSError as exc:
            logger.debug(
                "Unexpected OSError checking liveness of pid %s: %s", pid, exc
            )
            return False
        return True


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

    def snapshot(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

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


class LocalFeatureFileReader:
    """Filesystem-backed reader for the repository's feature file catalog."""

    def discover_feature_files(self, repo_root: Path) -> list[str]:
        return parser.discover_feature_files(repo_root)

    def discover_feature_details(
        self, repo_root: Path
    ) -> list[parser.FeatureDetail]:
        return parser.discover_feature_details(repo_root)


class LocalJobResultStoreFactory:
    """Creates filesystem-backed job result stores bound to a log dir."""

    def bind(self, log_dir: Path) -> "LocalJobResultStore":
        return LocalJobResultStore(log_dir)


class LocalJobResultStore:
    """Filesystem-backed job results rooted at one log directory.

    The ``{job_id}_*`` file layout is private here; callers address results
    by ``job_id`` only.
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir

    def _paths(self, job_id: str) -> domain.JobArtifactPaths:
        return domain.job_artifact_paths(self._log_dir, job_id)

    def write_targets(self, job_id: str) -> WriteTargets:
        paths = self._paths(job_id)
        return WriteTargets(
            stdout_log=paths.stdout_log, json_report=paths.json_report
        )

    def artifacts(self, job_id: str) -> Artifacts:
        paths = self._paths(job_id)
        return Artifacts(
            log_dir=str(self._log_dir),
            stdout_log=str(paths.stdout_log),
            json_report=str(paths.json_report),
            metadata=str(paths.metadata),
        )

    def read_metadata(self, job_id: str) -> dict[str, Any]:
        path = self._paths(job_id).metadata
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_metadata(self, job_id: str, payload: dict[str, Any]) -> None:
        path = self._paths(job_id).metadata
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

    def append_event(self, event: dict[str, Any]) -> None:
        index_path = self._log_dir / domain.JOB_INDEX_FILE_NAME
        try:
            with index_path.open("a", encoding="utf-8") as index_stream:
                index_stream.write(
                    json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
                )
        except OSError:
            # Index write failures should not break job execution/status.
            return

    def log_tail(self, job_id: str, lines: int) -> str:
        path = self._paths(job_id).stdout_log
        if not path.exists():
            return "Waiting for output..."

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            tail = deque(stream, maxlen=lines)
        return "".join(tail).rstrip() if tail else "Waiting for output..."

    def log_tail_lines(self, job_id: str, lines: int) -> list[str]:
        path = self._paths(job_id).stdout_log
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            tail = deque(stream, maxlen=lines)
        return [line.rstrip("\n") for line in tail]

    def read_report(self, job_id: str) -> list[Any] | None:
        path = self._paths(job_id).json_report
        if not path.exists():
            return None

        try:
            report_data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(report_data, list):
            return None

        return report_data

    def exists(self, job_id: str) -> ExistsFlags:
        paths = self._paths(job_id)
        return ExistsFlags(
            stdout_log=paths.stdout_log.exists(),
            json_report=paths.json_report.exists(),
            metadata=paths.metadata.exists(),
        )

    def list_job_ids(self) -> list[str]:
        if not self._log_dir.exists():
            return []
        suffix = domain.METADATA_SUFFIX
        return sorted(
            path.name[: -len(suffix)]
            for path in self._log_dir.glob(f"*{suffix}")
        )


class LocalWorkspace:
    """Resolves repository paths and the subprocess environment at runtime."""

    def resolve_repo_root(self, override: str | None) -> Path:
        if override:
            return self._validated_repo_root(Path(override).expanduser())

        env_value = os.environ.get("UBUNTU_PRO_CLIENT_REPO")
        if env_value:
            return self._validated_repo_root(Path(env_value).expanduser())

        detected = self._detect_repo_root(Path(__file__).resolve())
        if detected is not None:
            return detected

        raise ValueError(
            "Could not determine repo_root: not running from within a "
            "source checkout of ubuntu-pro-client (e.g. installed via "
            "'uvx --from'). Pass repo_root explicitly or set "
            "UBUNTU_PRO_CLIENT_REPO."
        )

    def _detect_repo_root(self, start: Path) -> Path | None:
        for candidate in [start, *start.parents]:
            if (candidate / "features").exists() and (
                candidate / "tox.ini"
            ).exists():
                return candidate.resolve()
        return None

    def resolve_log_dir(self, repo_root: Path) -> Path:
        env_path = os.environ.get("MCP_LOG_DIR")
        if env_path:
            log_dir = Path(env_path).resolve()
        else:
            log_dir = repo_root / ".mcp_behave_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def subprocess_env(self) -> dict[str, str]:
        return os.environ.copy()

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
