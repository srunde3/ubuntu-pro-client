"""Port definitions (interfaces) for the campaign package.

These Protocols describe the external interactions ``CampaignService``
depends on. Concrete adapters live in ``behave_campaign.adapters``; tests may
inject fakes.

Small collaborators -- the clock, the git read -- are passed as plain
callables rather than Protocols, matching ``behave_mcp.service``.
"""

from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

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


class CampaignLockedError(CampaignError):
    """Raised when a campaign's run lock is held by someone else."""


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


class LaneStartError(CampaignError):
    """Raised when a lane could not be opened for a unit.

    ``full_pool`` names the pool that had no room, when that is the reason;
    the other pools may still have room.
    """

    def __init__(self, message: str, full_pool: Pool | None = None) -> None:
        super().__init__(message)
        self.full_pool = full_pool


class LaneRunner(Protocol):
    """Runs one test unit as a job, and reports when it has finished."""

    def start(self, unit: Unit, *, repo_root: Path, install_from: str) -> str:
        """Start a job for ``unit`` and return its job id.

        Raises LaneStartError when no job could be started, including when
        the unit's pool is at capacity.
        """
        ...

    def poll(self, job_id: str) -> Any | None:
        """Return the job's result payload, or None while it is running.

        The payload is whatever ``classify_result`` understands: the MCP's
        own completion shape.
        """
        ...

    def kill(self, job_id: str) -> bool:
        """Kill the job, and return whether the kill was sent.

        False means there was nothing to kill: the job had already ended,
        or its process could not be reached.
        """
        ...


class Ticker(Protocol):
    """Calls a function repeatedly until it reports it is done.

    A port so the runner can be driven synchronously in tests: the thread is
    an implementation of this, not a thing the runner owns.
    """

    def start(self, tick: Callable[[], bool]) -> None:
        """Begin calling ``tick``. Calling it again while running is a no-op.

        ``tick`` returning True means the work is finished and no further
        calls should be made.
        """
        ...

    def stop(self) -> None:
        """Stop calling ``tick`` and wait for any call in flight."""
        ...

    def is_running(self) -> bool:
        """Return whether ticks are still being delivered."""
        ...


class CampaignRunLock(Protocol):
    """Exclusive claim on a campaign, held while it is actively scheduling.

    Advisory: it binds processes that ask for it, which is every path this
    project ships. The holder's death releases it, so a crashed server
    strands nothing.
    """

    def acquire(self, campaign_id: str) -> None:
        """Take the lock.

        Raises CampaignLockedError if another holder has it.
        """
        ...

    def release(self, campaign_id: str) -> None:
        """Release the lock. Safe to call when not held."""
        ...

    def held_by_other(self, campaign_id: str) -> bool:
        """Return whether some other process currently holds the lock."""
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
