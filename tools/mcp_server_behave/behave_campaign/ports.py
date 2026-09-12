"""Port definitions (interfaces) for the campaign package.

These Protocols describe the external interactions ``CampaignService``
depends on. Concrete adapters live in ``behave_campaign.adapters``; tests may
inject fakes.

Small collaborators -- the clock, the git read -- are passed as plain
callables rather than Protocols, matching ``behave_mcp.service``.
"""

from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from behave_campaign.domain import (
    CampaignError,
    CampaignHeader,
    Event,
    Filters,
    NewEvent,
    PlanRecord,
    Record,
    Unit,
)


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
        header: CampaignHeader,
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

    def replay(self, campaign_id: str) -> list[Record]:
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


class FeatureReader(Protocol):
    """Reads the behave feature files a campaign draws its units from."""

    def discover_units(self, repo_root: Path, filters: Filters) -> list[Unit]:
        """Return every unit in the feature files matching ``filters``.

        Raises CampaignError for an unknown release, machine type, or
        feature rather than silently matching nothing.
        """
        ...

    def available_dimensions(self, repo_root: Path) -> dict[str, Any]:
        """Return the releases and machine types the feature files can run."""
        ...


class LaneStartError(CampaignError):
    """Raised when a lane could not be opened for a unit."""


class LaneRunner(Protocol):
    """Runs one test unit as a job, and reports when it has finished."""

    def start(self, unit: Unit, *, repo_root: Path, install_from: str) -> str:
        """Start a job for ``unit`` and return its job id.

        Raises LaneStartError when no job could be started, including when
        the runner is at capacity.
        """
        ...

    def poll(self, job_id: str) -> Any | None:
        """Return the job's result payload, or None while it is running.

        The payload is whatever ``classify_result`` understands: the MCP's
        own completion shape.
        """
        ...


class EventLog(Protocol):
    """Numbered, durable notifications about one campaign.

    The campaign's own record is the truth about what happened; this is how
    a watcher hears about it promptly. ``seq`` is monotonic per campaign and
    survives a restart, so a cursor stays valid across one.
    """

    def append(
        self, campaign_id: str, events: Sequence[NewEvent]
    ) -> list[Event]:
        """Number and store ``events``, returning them as stored."""
        ...

    def read(
        self,
        campaign_id: str,
        *,
        since_seq: int,
        kinds: Sequence[str],
        limit: int,
    ) -> list[Event]:
        """Return matching events after ``since_seq``, oldest first."""
        ...

    def wait(
        self,
        campaign_id: str,
        *,
        since_seq: int,
        kinds: Sequence[str],
        limit: int,
        timeout: float,
    ) -> list[Event]:
        """Like ``read``, but block until something matches or time runs out.

        Returns an empty list on timeout rather than raising: a quiet
        campaign is not an error.
        """
        ...

    def latest_seq(self, campaign_id: str) -> int:
        """Return the highest seq stored, or 0 when there are none."""
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
