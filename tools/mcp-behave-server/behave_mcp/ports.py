"""Port definitions (interfaces) for the behave MCP server.

These Protocols describe the external interactions the application core depends
on. Concrete adapters live in ``behave_mcp.adapters``; tests may inject fakes.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from behave_mcp.messages import (
    Combo,
    Dimensions,
    FeatureCatalogEntry,
    FeatureDetail,
    ScenarioSummary,
)


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
    stdout_log: Path
    json_report: Path
    metadata: Path
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


class FeatureCatalog(Protocol):
    """Pure catalog/filtering operations over parsed feature data.

    These are owned by the ``pro-client-features`` dependency, same as
    ``FeatureFileReader``, but are pure transformations rather than disk
    I/O, so they get their own port/adapter instead of being imported
    directly into ``domain.py``.
    """

    def normalize_feature_file_arg(self, feature_file: str) -> str:
        """Normalize a ``feature_file`` argument to its canonical form."""
        ...

    def catalog_entry(
        self, feature_detail: FeatureDetail
    ) -> FeatureCatalogEntry:
        """Project a full feature detail into a lightweight catalog entry."""
        ...

    def aggregate_dimensions(
        self, feature_details: list[FeatureDetail]
    ) -> Dimensions:
        """Return distinct releases and machine_types with scenario counts."""
        ...

    def scenario_matches(
        self,
        scenario: ScenarioSummary,
        feature_tags: list[str],
        *,
        release: str | None = None,
        machine_type: str | None = None,
        tag: str | None = None,
        text: str | None = None,
    ) -> bool:
        """Return whether a scenario satisfies all provided filters."""
        ...

    def filtered_combos(
        self,
        scenario: ScenarioSummary,
        release: str | None = None,
        machine_type: str | None = None,
    ) -> list[Combo]:
        """Return the scenario combos matching release/machine_type."""
        ...


class ArtifactStore(Protocol):
    """Filesystem persistence for job logs, metadata, and reports."""

    def read_metadata(self, path: Path) -> dict[str, Any]:
        """Return parsed metadata, or an empty dict if missing/invalid."""
        ...

    def write_metadata(self, path: Path, payload: dict[str, Any]) -> None:
        """Write metadata as sorted, indented JSON with a trailing newline."""
        ...

    def append_index_event(self, log_dir: Path, event: dict[str, Any]) -> None:
        """Append one sorted JSON line to the per-log-dir index file."""
        ...

    def tail_file(self, path: Path, lines: int) -> str:
        """Return the last ``lines`` lines of ``path`` as a single string."""
        ...

    def tail_lines(self, path: Path, lines: int) -> list[str]:
        """Return the last ``lines`` lines of ``path`` as a list."""
        ...

    def read_report_json(self, path: Path) -> list[Any] | None:
        """Return the behave JSON report list, or None if missing/invalid."""
        ...

    def exists(self, path: Path) -> bool:
        """Return whether ``path`` exists."""
        ...

    def list_job_ids(self, log_dir: Path) -> list[str]:
        """Return job ids discovered from metadata files under ``log_dir``."""
        ...


class Workspace(Protocol):
    """Resolves repository paths and the subprocess environment at runtime."""

    def resolve_repo_root(self, override: str | None) -> Path:
        """Resolve the repository root, raising ValueError if invalid."""
        ...

    def resolve_log_dir(self, repo_root: Path) -> Path:
        """Resolve and create the directory for job artifacts."""
        ...

    def subprocess_env(self) -> dict[str, str]:
        """Return the environment to forward to the behave subprocess."""
        ...
