"""Concrete adapters implementing the ports in ``behave_mcp.ports``."""

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from behave_mcp import parser
from behave_mcp.domain import layout
from behave_mcp.domain.pools import Pool
from behave_mcp.messages import (
    Artifacts,
    ExistsFlags,
    JobExit,
    JobRecord,
    RepoState,
)
from behave_mcp.ports import JobStartError, Lease, WriteTargets

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _try_lock(path: Path) -> int | None:
    """Open ``path`` and take its exclusive lock without waiting.

    Returns the open descriptor holding the lock, or None if another
    open file holds it. A lock taken this way lasts until the descriptor
    and every copy of it are closed -- in this process or a child that
    inherited it -- which the kernel does however the holder dies.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _is_locked(path: Path) -> bool:
    """Whether some open file holds ``path``'s lock right now."""
    if not path.exists():
        return False
    fd = _try_lock(path)
    if fd is None:
        return True
    os.close(fd)
    return False


class FileLease:
    """A lane held through its lock file's descriptor."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class FileLanePool:
    """Lanes as lock files, ``lanes/<pool>/<n>.lock`` for n below a limit.

    The kernel arbitrates, so every process on one state directory shares
    the same lanes without talking to the others.
    """

    def __init__(self, lanes_dir: Callable[[], Path]) -> None:
        self._lanes_dir = lanes_dir

    def take(self, pool: Pool, limit: int) -> FileLease | None:
        for lane in range(limit):
            fd = _try_lock(self._path(pool, lane))
            if fd is not None:
                return FileLease(fd)
        return None

    def busy(self, pool: Pool, limit: int) -> int:
        return sum(
            1 for lane in range(limit) if _is_locked(self._path(pool, lane))
        )

    def _path(self, pool: Pool, lane: int) -> Path:
        return layout.lane_path(self._lanes_dir(), pool.value, lane)


def _wrapper_command(job_dir: Path, command: list[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "behave_mcp.wrapper",
        str(job_dir),
        "--",
        *command,
    ]


class LockedJobLauncher:
    """Starts each job under ``behave_mcp.wrapper``, which holds the lane
    and the job's own lock for its whole life.

    Whether a job is alive is whether its lock is held, which any process
    can ask. Once the wrapper can take a kill, it writes its PID into the
    lock file, so any process can also signal it -- trusted only while the
    lock is held, since a held lock means the wrapper is alive and its PID
    cannot have been reused.
    """

    def __init__(self, jobs_dir: Callable[[], Path]) -> None:
        self._jobs_dir = jobs_dir

    def launch(
        self,
        job_id: str,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        lease: Lease,
    ) -> None:
        paths = layout.job_paths(self._jobs_dir(), job_id)
        job_fd: int | None = None
        try:
            job_fd = _try_lock(paths.lock)
            if job_fd is None:
                raise JobStartError("job {} is already locked".format(job_id))
            lane_fd = lease.fd if isinstance(lease, FileLease) else None
            inherited = tuple(fd for fd in (lane_fd, job_fd) if fd is not None)
            try:
                subprocess.Popen(
                    _wrapper_command(paths.job_dir, command),
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    # Its own session, so this process's death or a signal
                    # to its group never reaches the job.
                    start_new_session=True,
                    pass_fds=inherited,
                )
            except OSError as exc:
                raise JobStartError(str(exc)) from exc
        finally:
            # From here only the wrapper holds the locks.
            if job_fd is not None:
                os.close(job_fd)
            lease.close()

    def is_running(self, job_id: str) -> bool:
        return _is_locked(self._paths(job_id).lock)

    def signal(self, job_id: str) -> bool:
        """Kill a running job.

        The kill is left as a marker first. A wrapper still starting up has
        not written its PID yet; it reads the marker before it starts the
        job, so a kill is never lost to the race.
        """
        paths = self._paths(job_id)
        if not _is_locked(paths.lock):
            return False
        paths.kill.touch()
        try:
            pid = int(paths.lock.read_text().strip())
        except ValueError:
            return True
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def _paths(self, job_id: str) -> layout.JobPaths:
        return layout.job_paths(self._jobs_dir(), job_id)


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


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` whole or not at all.

    A temporary file in the same directory is synced, then renamed over
    the target, so a reader never sees half a file and a crash leaves at
    most a stray temporary file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".{}.".format(path.name)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _json_text(model: BaseModel) -> str:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _read_model(path: Path, model: type[ModelT]) -> ModelT | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        return None


class LocalJobResultStore:
    """Filesystem-backed job results: one directory per job under the
    jobs directory, laid out by ``layout.job_paths``. Callers address
    results by ``job_id`` only.
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir

    def _paths(self, job_id: str) -> layout.JobPaths:
        return layout.job_paths(self._log_dir, job_id)

    def write_targets(self, job_id: str) -> WriteTargets:
        paths = self._paths(job_id)
        paths.job_dir.mkdir(parents=True, exist_ok=True)
        return WriteTargets(
            stdout_log=paths.stdout_log, json_report=paths.json_report
        )

    def artifacts(self, job_id: str) -> Artifacts:
        paths = self._paths(job_id)
        return Artifacts(
            job_dir=str(paths.job_dir),
            record=str(paths.record),
            status=str(paths.status),
            stdout_log=str(paths.stdout_log),
            json_report=str(paths.json_report),
        )

    def read_record(self, job_id: str) -> JobRecord:
        return (
            _read_model(self._paths(job_id).record, JobRecord) or JobRecord()
        )

    def write_record(self, job_id: str, record: JobRecord) -> None:
        try:
            _write_atomic(self._paths(job_id).record, _json_text(record))
        except OSError:
            # A record that cannot be written must not stop the job.
            return

    def read_exit(self, job_id: str) -> JobExit | None:
        return _read_model(self._paths(job_id).status, JobExit)

    def write_exit(self, job_id: str, exit: JobExit) -> JobExit:
        written = self.read_exit(job_id)
        if written is not None:
            return written
        try:
            _write_atomic(self._paths(job_id).status, _json_text(exit))
        except OSError:
            # The exit is still reported; it is recorded on a later look.
            pass
        return exit

    def log_tail(self, job_id: str, lines: int) -> str:
        path = self._paths(job_id).stdout_log
        if not path.exists():
            return "Waiting for output..."

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            tail = deque(stream, maxlen=lines)
        return "".join(tail).rstrip() if tail else "Waiting for output..."

    def read_log_lines(self, job_id: str) -> list[str]:
        path = self._paths(job_id).stdout_log
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        return text.rstrip("\n").split("\n") if text else []

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
            record=paths.record.exists(),
            status=paths.status.exists(),
            stdout_log=paths.stdout_log.exists(),
            json_report=paths.json_report.exists(),
        )

    def list_job_ids(self) -> list[str]:
        """Every job with a launch record: ``jobs/*/job.json``."""
        if not self._log_dir.exists():
            return []
        return sorted(
            path.parent.name
            for path in self._log_dir.glob(
                "*/{}".format(layout.JOB_RECORD_NAME)
            )
        )


class LocalWorkspace:
    """Resolves repository paths and the subprocess environment at runtime."""

    def resolve_repo_root(self, override: str | None) -> Path:
        if override:
            return self._validated_repo_root(Path(override).expanduser())

        env_value = os.environ.get(layout.REPO_ENV_VAR)
        if env_value:
            return self._validated_repo_root(Path(env_value).expanduser())

        detected = self._detect_repo_root(Path(__file__).resolve())
        if detected is not None:
            return detected

        raise ValueError(
            "Could not determine repo_root: not running from within a "
            "source checkout of ubuntu-pro-client (a non-editable "
            "install). Pass repo_root explicitly or set "
            "UBUNTU_PRO_CLIENT_REPO."
        )

    def _detect_repo_root(self, start: Path) -> Path | None:
        for candidate in [start, *start.parents]:
            if (candidate / "features").exists() and (
                candidate / "tox.ini"
            ).exists():
                return candidate.resolve()
        return None

    def resolve_state_dir(self) -> Path:
        """Where everything this server writes goes.

        ``$MCP_STATE_DIR``, else the server's own checkout's state
        directory. A per-call ``repo_root`` never moves it.
        """
        configured = os.environ.get(layout.STATE_DIR_ENV_VAR)
        if configured:
            return Path(configured).expanduser().resolve()
        return layout.state_dir(self.resolve_repo_root(None), None)

    def resolve_log_dir(self) -> Path:
        return self._subdir(layout.JOBS_SUBDIR)

    def resolve_campaign_dir(self) -> Path:
        return self._subdir(layout.CAMPAIGNS_SUBDIR)

    def _subdir(self, name: str) -> Path:
        path = self.resolve_state_dir() / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def subprocess_env(self) -> dict[str, str]:
        return os.environ.copy()

    def repo_state(self, repo_root: Path) -> RepoState:
        commit = self._git(repo_root, "rev-parse", "HEAD")
        if commit is None:
            return RepoState()
        branch = self._git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        status = self._git(repo_root, "status", "--porcelain")
        return RepoState(
            commit=commit,
            branch=branch,
            dirty=bool(status) if status is not None else None,
        )

    @staticmethod
    def _git(repo_root: Path, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            # Best-effort by contract (see Workspace.repo_state): swallow
            # anything, including e.g. a test's global Popen monkeypatch
            # meant for the behave launcher, not this git shell-out.
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

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
