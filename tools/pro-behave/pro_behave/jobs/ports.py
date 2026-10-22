"""Port definitions (interfaces) for the job service.

These Protocols describe the external interactions ``JobService`` depends
on. Concrete adapters live in ``pro_behave.jobs.adapters``; tests may
inject fakes.
"""

from pathlib import Path
from typing import Any, Callable, ContextManager, NamedTuple, Protocol

from pro_behave.jobs.domain.config import CheckoutConfig
from pro_behave.jobs.domain.pools import Pool
from pro_behave.jobs.messages import (
    Artifacts,
    ExistsFlags,
    JobExit,
    JobRecord,
    JobRequest,
    RepoState,
)
from pro_behave.jobs.parser import FeatureDetail


class JobStartError(Exception):
    """Raised by a JobLauncher when a job's process cannot be started."""


class Lease(Protocol):
    """A lane, held. The holder releases it by closing it."""

    def close(self) -> None:
        """Let go of the lane. Safe to call more than once."""
        ...


class LanePool(Protocol):
    """Lanes shared by every process using one state directory."""

    def take(self, pool: Pool, max_jobs: int) -> Lease | None:
        """Take a free lane in ``pool``, or return None if ``max_jobs`` or
        more of its lanes are held."""
        ...

    def busy(self, pool: Pool) -> int:
        """Return how many of ``pool``'s lanes are held, whatever their
        number, so a lowered max jobs still counts the jobs already
        running."""
        ...


class JobLauncher(Protocol):
    """Starts a job under a job process that outlives this process, and
    reads and kills it from any process."""

    def launch(
        self,
        job_id: str,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        lease: Lease,
        record: Callable[[], None],
    ) -> None:
        """Start ``command`` for ``job_id`` under a job process that takes
        over ``lease`` and the job's own lock for as long as it lives.

        ``record`` writes the launch record. It is called once the job's
        lock is held and before the job process exists, so the record is
        never seen without a held lock while the launch is in progress.
        The launcher's own hold on ``lease`` is closed either way. Raises
        JobStartError if the job process cannot be started.
        """
        ...

    def is_running(self, job_id: str) -> bool:
        """Return whether the job's process is alive: its lock is held."""
        ...

    def kill(self, job_id: str) -> bool:
        """Ask a running job to stop; return whether the kill was sent."""
        ...


class ReconcileLock(Protocol):
    """One lock per state directory, held while reconcile runs."""

    def hold(self) -> ContextManager[None]:
        """Wait for the lock and hold it for the ``with`` block."""
        ...


class ConfigStore(Protocol):
    """The service's config for this checkout."""

    def read(self) -> CheckoutConfig:
        """Return the config, or the defaults if none is stored. Raises
        ValueError naming the problem if the stored one is invalid."""
        ...

    def write(self, config: CheckoutConfig) -> None:
        """Store the config, whole or not at all."""
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

    def read_request(self, job_id: str) -> JobRequest | None:
        """Return what the job is to run, or None if it was never queued."""
        ...

    def write_request(self, job_id: str, request: JobRequest) -> None:
        """Queue the job: write what it is to run, once."""
        ...

    def read_record(self, job_id: str) -> JobRecord:
        """Return the job's record, or an empty one if missing/invalid."""
        ...

    def write_record(self, job_id: str, record: JobRecord) -> None:
        """Write the job's launch record, once."""
        ...

    def read_exit(self, job_id: str) -> JobExit | None:
        """Return how the job ended, or None if no exit is recorded."""
        ...

    def write_exit(self, job_id: str, exit: JobExit) -> JobExit:
        """Record how the job ended, unless an exit already is; return the
        exit on disk, so the first writer wins."""
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
        """Return every job ever queued at this location."""
        ...


class JobResultStoreFactory(Protocol):
    """Binds a ``JobResultStore`` to a resolved results location."""

    def bind(self, jobs_dir: Path) -> JobResultStore:
        """Return a store rooted at ``jobs_dir``."""
        ...


class Workspace(Protocol):
    """Resolves repository paths and the subprocess environment at runtime."""

    def resolve_repo_root(self, override: str | None) -> Path:
        """Resolve the repository root, raising ValueError if invalid."""
        ...

    def resolve_jobs_dir(self) -> Path:
        """Resolve and create the server's directory for job artifacts.

        It is under the server's own state directory, whatever checkout a
        call names: one server keeps one set of jobs.
        """
        ...

    def resolve_campaign_dir(self) -> Path:
        """Resolve and create the server's directory for campaign files."""
        ...

    def subprocess_env(self) -> dict[str, str]:
        """Return the environment to forward to the behave subprocess."""
        ...

    def repo_state(self, repo_root: Path) -> RepoState:
        """Return repo_root's git commit/branch/dirty state, best-effort.

        Returns only the root when repo_root isn't a git checkout
        or git isn't available -- never raises.
        """
        ...
