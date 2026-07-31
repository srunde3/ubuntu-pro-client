"""Concrete adapters implementing the ports in ``behave_mcp.ports``."""

import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from behave.parser import parse_file
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


class LocalFeatureFileReader:
    """Filesystem-backed reader for the repository's feature file catalog."""

    def __init__(self) -> None:
        # Cache parsed feature summaries keyed by absolute path, invalidated by
        # file mtime so all browse tools share a single parse pass.
        self._detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def discover_feature_files(self, repo_root: Path) -> list[str]:
        features_dir = repo_root / "features"
        if not features_dir.exists():
            return []

        return sorted(
            str(path.relative_to(repo_root)).replace("\\", "/")
            for path in features_dir.rglob("*.feature")
        )

    def discover_feature_details(
        self, repo_root: Path
    ) -> list[dict[str, Any]]:
        features_dir = repo_root / "features"
        if not features_dir.exists():
            return []

        details: list[dict[str, Any]] = []
        for path in sorted(features_dir.rglob("*.feature")):
            summary = self._read_feature_detail(path)
            if summary is None:
                continue
            rel_path = str(path.relative_to(repo_root)).replace("\\", "/")
            details.append({"path": rel_path, **summary})
        return sorted(details, key=lambda detail: detail["path"])

    def _read_feature_detail(self, path: Path) -> dict[str, Any] | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None

        cache_key = str(path)
        cached = self._detail_cache.get(cache_key)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        try:
            feature = parse_file(str(path))
        except Exception:
            # A single unparseable feature must not break the whole catalog.
            return None
        if feature is None:
            return None

        summary = domain.summarize_feature(feature)
        self._detail_cache[cache_key] = (mtime, summary)
        return summary


class LocalArtifactStore:
    """Filesystem-backed artifact persistence and log tailing."""

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


class LocalWorkspace:
    """Resolves repository paths and the subprocess environment at runtime."""

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
