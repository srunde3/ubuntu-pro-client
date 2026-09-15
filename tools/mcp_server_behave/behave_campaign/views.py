"""Build the response DTOs from campaign records.

Shared by the service and the runner so that one campaign fact has one
outward shape.
"""

from __future__ import annotations

from typing import Sequence

from behave_campaign import domain
from behave_campaign.domain import Record, UnitStatus
from behave_campaign.messages import (
    AttemptView,
    CampaignHeader,
    CampaignListing,
    CampaignRepo,
    CampaignScope,
    CampaignState,
    ScopeSize,
    StateCounts,
    UnitHistoryView,
    UnitView,
)


def header_of(records: Sequence[Record]) -> domain.CampaignHeader | None:
    for record in records:
        if isinstance(record, domain.CampaignHeader):
            return record
    return None


def unit_view(status: UnitStatus) -> UnitView:
    return UnitView(
        feature=status.unit.feature,
        scenario=status.unit.scenario,
        release=status.unit.release,
        machine_type=status.unit.machine_type,
        state=status.state,
        job_id=status.job_id,
        attempt_count=len(status.attempts),
    )


def unit_history_view(status: UnitStatus) -> UnitHistoryView:
    return UnitHistoryView(
        **unit_view(status).model_dump(),
        attempts=[
            AttemptView(**attempt.as_dict()) for attempt in status.attempts
        ],
    )


def campaign_header(
    campaign_id: str, records: Sequence[Record]
) -> CampaignHeader:
    header = header_of(records) or domain.CampaignHeader()
    return CampaignHeader(
        campaign_id=campaign_id,
        created_at=header.at,
        install_from=header.install_from,
        max_lanes=header.max_lanes,
        total_units=len(domain.reduce_units(records)),
        repo=CampaignRepo(**header.repo.as_dict()),
        scope=CampaignScope(**header.filters.scope_as_dict()),
    )


def campaign_state(
    campaign_id: str,
    records: Sequence[Record],
    counted: Sequence[UnitStatus] | None = None,
) -> CampaignState:
    """The campaign's state; ``counted`` narrows the counts to a selection.

    ``lifecycle`` and ``lanes_busy`` are facts about the whole campaign and
    never narrow.
    """
    statuses = domain.reduce_units(records)
    return CampaignState(
        campaign_id=campaign_id,
        lifecycle=domain.lifecycle(records),
        lanes_busy=len(domain.running(statuses)),
        counts=StateCounts(
            **domain.count_states(statuses if counted is None else counted)
        ),
    )


def campaign_listing(
    campaign_id: str, records: Sequence[Record]
) -> CampaignListing:
    header = header_of(records) or domain.CampaignHeader()
    scope = header.filters
    return CampaignListing(
        **campaign_state(campaign_id, records).model_dump(),
        created_at=header.at,
        install_from=header.install_from,
        max_lanes=header.max_lanes,
        total_units=len(domain.reduce_units(records)),
        scope_size=ScopeSize(
            features=len(scope.feature),
            scenarios=len(scope.scenario),
            releases=len(scope.release),
            machine_types=len(scope.machine_type),
        ),
    )
