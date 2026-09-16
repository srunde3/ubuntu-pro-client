"""Port definitions (interfaces) for the behave MCP server.

These Protocols describe the external interactions the application core depends
on. Concrete adapters live in ``behave_mcp.adapters``; tests may inject fakes.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from behave_mcp.messages import Artifacts, ExistsFlags, JobRecord, RepoState
from behave_mcp.parser import FeatureDetail


class LogFileOpenError(Exception):
    """Raised by a ProcessLauncher when the stdout log cannot be opened."""


class ProcessStartError(Exception):
    """Raised by a ProcessLauncher when the child process cannot be started."""


class ProcessHandle(Protocol):
    """Handle to a launched behave subprocess and its owned stdout log file."""

    @property
    def pid(self) -> int:
        """Return the OS process id of the underlying process."""
        ...

    def poll(self) -> int | None:
        """Return the exit code, or None while the process is still running."""
        ...

    def close(self) -> None:
        """Close the owned stdout log file. Safe to call more than once."""
        ...

    def terminate(self) -> None:
        """Terminate the underlying process."""
        ...


class ProcessLauncher(Protocol):
    """Launches a behave subprocess writing combined output to a log file."""

    def launch(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        stdout_log_path: Path,
    ) -> ProcessHandle:
        """Open ``stdout_log_path`` for writing and start ``command``.

        Raises LogFileOpenError if the log file cannot be opened and
        ProcessStartError if the process cannot be started.
        """
        ...

    def is_pid_alive(self, pid: int) -> bool:
        """Return whether ``pid`` still refers to a live OS process.

        Used to classify jobs recovered from disk after a server restart,
        where no in-process ``ProcessHandle`` survives to ``poll()``.
        """
        ...


@dataclass
class Job:
    """In-memory record for a reserved, running, or recovered behave job."""

    job_id: str
    process_handle: ProcessHandle | None
    log_dir: Path
    reserved: bool = False
    pid: int | None = None


@dataclass
class ReservationResult:
    """Outcome of an atomic capacity check + slot reservation."""

    reserved: bool
    running_jobs: int
    max_parallel: int


class JobRegistry(Protocol):
    """Thread-safe registry tracking reserved and running behave jobs."""

    def try_reserve(self, job: Job, max_parallel: int) -> ReservationResult:
        """Atomically count active jobs and reserve a slot if there is room."""
        ...

    def register(self, job_id: str, job: Job) -> None:
        """Replace any existing entry for ``job_id`` with ``job``."""
        ...

    def release(self, job_id: str) -> None:
        """Remove the entry for ``job_id`` (used on launch failure)."""
        ...

    def get(self, job_id: str) -> Job | None:
        """Return the stored job or None."""
        ...

    def snapshot(self) -> list[Job]:
        """Return a point-in-time copy of every tracked job."""
        ...

    def clear(self) -> None:
        """Remove all entries (primarily for tests)."""
        ...


class FeatureFileReader(Protocol):
    """Reads the catalog of behave feature files from the repository."""

    def discover_feature_files(self, repo_root: Path) -> list[str]:
        """Return repo-relative paths of every ``*.feature`` file, sorted."""
        ...

    def discover_feature_details(self, repo_root: Path) -> list[FeatureDetail]:
        """Return parsed metadata for every feature file, sorted by path.

        Each entry is a ``FeatureDetail`` whose ``path`` is set to the
        repo-relative feature path. Files that fail to parse are skipped.
        """
        ...


class WriteTargets(NamedTuple):
    """The two files the behave subprocess writes to for a job."""

    stdout_log: Path
    json_report: Path


class JobResultStore(Protocol):
    """A job's results, addressed by ``job_id``."""

    def write_targets(self, job_id: str) -> WriteTargets:
        """Return the files the subprocess writes stdout and the report to."""
        ...

    def artifacts(self, job_id: str) -> Artifacts:
        """Return the artifact locations to surface to the caller."""
        ...

    def read_record(self, job_id: str) -> JobRecord:
        """Return the job's record, or an empty one if missing/invalid."""
        ...

    def write_record(self, job_id: str, record: JobRecord) -> None:
        """Persist the job's record."""
        ...

    def append_event(self, event: dict[str, Any]) -> None:
        """Append one event to this location's job index."""
        ...

    def log_tail(self, job_id: str, lines: int) -> str:
        """Return the last ``lines`` lines of the job's stdout as a string."""
        ...

    def read_log_lines(self, job_id: str) -> list[str]:
        """Return every line of the job's stdout; empty when there is none."""
        ...

    def read_report(self, job_id: str) -> list[Any] | None:
        """Return the behave JSON report list, or None if missing/invalid."""
        ...

    def exists(self, job_id: str) -> ExistsFlags:
        """Return which of the job's artifacts currently exist."""
        ...

    def list_job_ids(self) -> list[str]:
        """Return every job id discovered at this location."""
        ...


class JobResultStoreFactory(Protocol):
    """Binds a ``JobResultStore`` to a resolved results location."""

    def bind(self, log_dir: Path) -> JobResultStore:
        """Return a store rooted at ``log_dir``."""
        ...


class Workspace(Protocol):
    """Resolves repository paths and the subprocess environment at runtime."""

    def resolve_repo_root(self, override: str | None) -> Path:
        """Resolve the repository root, raising ValueError if invalid."""
        ...

    def resolve_log_dir(self, repo_root: Path) -> Path:
        """Resolve and create the directory for job artifacts."""
        ...

    def resolve_campaign_dir(self, repo_root: Path) -> Path:
        """Resolve and create the directory holding campaign files."""
        ...

    def subprocess_env(self) -> dict[str, str]:
        """Return the environment to forward to the behave subprocess."""
        ...

    def repo_state(self, repo_root: Path) -> RepoState:
        """Return repo_root's git commit/branch/dirty state, best-effort.

        Returns an all-None RepoState when repo_root isn't a git checkout
        or git isn't available -- never raises.
        """
        ...
