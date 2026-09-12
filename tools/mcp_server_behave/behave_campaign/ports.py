"""Port definitions (interfaces) for the campaign package.

These Protocols describe the external interactions ``CampaignService``
depends on. Concrete adapters live in ``behave_campaign.adapters``; tests may
inject fakes.

Small collaborators -- the clock, the git read -- are passed as plain
callables rather than Protocols, matching ``behave_mcp.service``.
"""

from pathlib import Path
from typing import Any, Protocol, Sequence

from behave_campaign.domain import (
    CampaignError,
    CampaignHeader,
    Filters,
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
