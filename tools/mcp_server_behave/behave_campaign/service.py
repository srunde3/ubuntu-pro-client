"""Orchestrates the campaign domain and ports into tool behaviours.

``CampaignService`` is driven by the MCP tool wrappers in
``behave_mcp.server`` and by ``behave_campaign.cli``. Neither the clock nor
the filesystem is reached directly, so the whole service is exercisable with
fakes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from behave_campaign import domain
from behave_campaign.domain import (
    CampaignError,
    CampaignHeader,
    Filters,
    PlanRecord,
    Record,
    RepoState,
    UnitStatus,
)
from behave_campaign.messages import (
    DEFAULT_UNITS_LIMIT,
    MAX_UNITS_LIMIT,
    CampaignRepo,
    CampaignScope,
    CampaignStatusResponse,
    CampaignSummary,
    CreateCampaignResponse,
    ListCampaignsResponse,
    StateCounts,
    UnitView,
)
from behave_campaign.ports import CampaignStore, FeatureReader


def _unit_view(status: UnitStatus) -> UnitView:
    return UnitView(
        feature=status.unit.feature,
        scenario=status.unit.scenario,
        release=status.unit.release,
        machine_type=status.unit.machine_type,
        state=status.state,
        job_id=status.job_id,
        attempt_count=len(status.attempts),
    )


def _counts(statuses: Sequence[UnitStatus]) -> StateCounts:
    return StateCounts(**domain.count_states(statuses))


def _summary(
    campaign_id: str,
    header: CampaignHeader | None,
    statuses: Sequence[UnitStatus],
) -> CampaignSummary:
    header = header or CampaignHeader()
    return CampaignSummary(
        campaign_id=campaign_id,
        created_at=header.at,
        install_from=header.install_from,
        max_lanes=header.max_lanes,
        total_units=len(statuses),
        counts=_counts(statuses),
        repo=CampaignRepo(**header.repo.as_dict()),
        scope=CampaignScope(**header.filters.scope_as_dict()),
    )


def _header_of(records: Sequence[Record]) -> CampaignHeader | None:
    for record in records:
        if isinstance(record, CampaignHeader):
            return record
    return None


class CampaignService:
    """Create, list and report on campaigns."""

    def __init__(
        self,
        *,
        store: CampaignStore,
        features: FeatureReader,
        now: Callable[[], str],
        repo_state: Callable[[Path], RepoState],
        max_parallel_jobs: int,
    ) -> None:
        self._store = store
        self._features = features
        self._now = now
        self._repo_state = repo_state
        self._max_parallel_jobs = max_parallel_jobs

    def create_campaign(
        self,
        *,
        campaign_id: str,
        repo_root: Path,
        releases: Sequence[str] = (),
        machine_types: Sequence[str] = (),
        features: Sequence[str] = (),
        scenarios: Sequence[str] = (),
        install_from: str = domain.INSTALL_SOURCES[0],
        max_lanes: int = 1,
    ) -> CreateCampaignResponse:
        """Plan a campaign and store it. Starts nothing."""
        domain.validate_campaign_id(campaign_id)
        domain.validate_install_source(install_from)
        self._validate_max_lanes(max_lanes)

        filters = Filters(
            feature=tuple(features),
            scenario=tuple(scenarios),
            release=tuple(releases),
            machine_type=tuple(machine_types),
        )
        units = self._features.discover_units(repo_root, filters)
        if not units:
            raise CampaignError("no test units matched the requested filters")

        at = self._now()
        header = CampaignHeader(
            at=at,
            campaign_id=campaign_id,
            repo=self._repo_state(repo_root),
            filters=filters,
            install_from=install_from,
            max_lanes=max_lanes,
        )
        plans = [PlanRecord(unit=unit, at=at) for unit in units]
        self._store.create(campaign_id, header, plans)

        statuses = domain.reduce_units([header, *plans])
        return CreateCampaignResponse(
            campaign=_summary(campaign_id, header, statuses)
        )

    def list_campaigns(self) -> ListCampaignsResponse:
        """Summarise every stored campaign."""
        summaries = []
        for campaign_id in self._store.list_ids():
            records = self._store.replay(campaign_id)
            summaries.append(
                _summary(
                    campaign_id,
                    _header_of(records),
                    domain.reduce_units(records),
                )
            )
        return ListCampaignsResponse(
            campaign_dir=str(self._store.root), campaigns=summaries
        )

    def campaign_status(
        self,
        *,
        campaign_id: str,
        filters: Filters = Filters(),
        include_units: bool = False,
        limit: int = DEFAULT_UNITS_LIMIT,
    ) -> CampaignStatusResponse:
        """Report counts, the units in flight, and the units needing action.

        ``filters`` narrows which units are counted and listed, so a caller
        can ask about one release without reading the whole campaign.
        """
        records = self._store.replay(campaign_id)
        statuses = [
            status
            for status in domain.reduce_units(records)
            if filters.matches(status)
        ]

        capped, clamped = self._cap(limit)
        units = None
        truncated = False
        if include_units:
            units = [_unit_view(status) for status in statuses[:capped]]
            truncated = len(statuses) > capped

        return CampaignStatusResponse(
            campaign=_summary(campaign_id, _header_of(records), statuses),
            running=[
                _unit_view(status) for status in domain.running(statuses)
            ],
            problems=[
                _unit_view(status) for status in domain.problems(statuses)
            ],
            units=units,
            truncated=truncated,
            limit_clamped=clamped,
        )

    def _validate_max_lanes(self, max_lanes: int) -> None:
        if max_lanes < 1:
            raise CampaignError("max_lanes must be a positive integer")
        if max_lanes > self._max_parallel_jobs:
            raise CampaignError(
                "max_lanes {} exceeds the server's concurrent job limit of "
                "{}. Such a campaign would run serially and look like a "
                "hang, so raise the server's limit or lower max_lanes.".format(
                    max_lanes, self._max_parallel_jobs
                )
            )

    @staticmethod
    def _cap(limit: int) -> tuple[int, bool]:
        if limit < 1:
            raise CampaignError("limit must be positive")
        if limit > MAX_UNITS_LIMIT:
            return MAX_UNITS_LIMIT, True
        return limit, False
