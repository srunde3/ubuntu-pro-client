"""Port definitions (interfaces) for the campaign package.

These Protocols describe the external interactions ``CampaignService``
depends on. Concrete adapters live in ``behave_campaign.adapters``; tests may
inject fakes.

Small collaborators -- the clock, the git read -- are passed as plain
callables rather than Protocols, matching ``behave_mcp.service``.
"""

from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Protocol, Sequence

from behave_campaign.domain.bundle import BundleEntry
from behave_campaign.domain.records import (
    CampaignError,
    CampaignRecord,
    PlanRecord,
    Record,
    Scope,
    Unit,
)
from behave_mcp.domain.pools import Pool


# These subclass CampaignError -- and so ValueError -- because they report a
# caller's mistake, not a bug: asking for a campaign that isn't there, or
# creating one twice. Front-ends can then handle every campaign failure in
# one place instead of enumerating store exceptions.
class CampaignExistsError(CampaignError):
    """Raised when creating a campaign whose id is already taken."""


class CampaignNotFoundError(CampaignError):
    """Raised when addressing a campaign that was never created."""


class CampaignStore(Protocol):
    """Append-only storage for campaigns, addressed by campaign id."""

    @property
    def root(self) -> Path:
        """Where this store keeps campaigns, for reporting to callers."""
        ...

    def create(
        self,
        campaign_id: str,
        header: CampaignRecord,
        plans: Sequence[PlanRecord],
    ) -> None:
        """Write the header and every plan record for a new campaign.

        Raises CampaignExistsError if the campaign id is already in use.
        """
        ...

    def append(self, campaign_id: str, records: Sequence[Record]) -> None:
        """Append records to an existing campaign.

        Raises CampaignNotFoundError if the campaign does not exist.
        """
        ...

    def read_records(self, campaign_id: str) -> list[Record]:
        """Return every record for a campaign, in the order written.

        Raises CampaignNotFoundError if the campaign does not exist.
        """
        ...

    def exists(self, campaign_id: str) -> bool:
        """Return whether a campaign with this id has been created."""
        ...

    def list_ids(self) -> list[str]:
        """Return every stored campaign id, sorted."""
        ...

    def path_for(self, campaign_id: str) -> Path:
        """Return the directory holding the campaign's files."""
        ...

    def wait(self, campaign_id: str, seen: int, timeout: float) -> None:
        """Block until the campaign holds more than ``seen`` records, or
        ``timeout`` seconds pass. Returns either way."""
        ...


class FeatureReader(Protocol):
    """Reads the behave feature files a campaign draws its units from."""

    def discover_units(self, repo_root: Path, scope: Scope) -> list[Unit]:
        """Return every unit in the feature files in ``scope``.

        Raises CampaignError for an unknown release, machine type, or
        feature rather than silently matching nothing.
        """
        ...

    def available_dimensions(self, repo_root: Path) -> dict[str, Any]:
        """Return the releases and machine types the feature files can run."""
        ...


class QueueRefused(CampaignError):
    """Raised when the job service will not queue a unit's job: its pool is
    disabled, or the unit no longer matches its feature files."""

    def __init__(self, message: str, pool: Pool) -> None:
        super().__init__(message)
        self.pool = pool


class JobRunner(Protocol):
    """The job service, as a campaign uses it: queue, read and kill jobs,
    under the state directory's one lock."""

    def lock(self) -> ContextManager[None]:
        """Hold the state directory's reconcile lock for the block.

        Re-entrant within a thread: what runs inside may take it again.
        """
        ...

    def new_job_id(self) -> str:
        """A fresh job id, to record before the job is queued."""
        ...

    def queue(
        self,
        job_id: str,
        unit: Unit,
        *,
        repo_root: Path,
        install_from: str,
        campaign_id: str,
        record: Callable[[], None],
    ) -> None:
        """Queue a job for ``unit`` under ``job_id``, starting nothing.

        ``record`` is called once the unit is accepted and before the job is
        queued, so the attempt is written first. Raises QueueRefused, having
        called nothing, when the job service will not queue it.
        """
        ...

    def poll(self, job_id: str) -> dict[str, Any]:
        """Return the job's current status payload, with ``state``.

        A job the job service never queued reads as interrupted, ``died``.
        """
        ...

    def kill(self, job_id: str) -> bool:
        """Kill the job, queued or running; return whether it was.

        False means there was nothing to kill: the job had already ended,
        or its process could not be reached.
        """
        ...

    def reconcile_jobs(self) -> None:
        """Start queued jobs on free lanes."""
        ...


class JobFiles(Protocol):
    """Finds what the behave jobs left behind, by job id."""

    def files(self, job_id: str) -> Mapping[str, Path]:
        """Return the job's files that exist, keyed by their bundle name.

        The names are those in ``bundle.JOB_FILES``. A file the job never
        wrote is absent from the mapping rather than an error.
        """
        ...


class BundleWriter(Protocol):
    """Writes an export bundle as one archive."""

    def write(
        self, output: Path, top: str, entries: Sequence[BundleEntry]
    ) -> list[str]:
        """Write ``entries`` under the directory ``top`` into ``output``.

        Returns the names of entries whose source file did not exist; those
        are left out. Never replaces an existing file: raises CampaignError
        instead, and leaves nothing behind on any failure.
        """
        ...
