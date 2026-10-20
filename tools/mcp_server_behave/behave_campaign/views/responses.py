"""Build the response DTOs from campaign records.

Shared by the service and the runner so that one campaign fact has one
outward shape.
"""

from __future__ import annotations

from typing import Sequence

from behave_campaign.domain.records import CampaignHeader as HeaderRecord
from behave_campaign.domain.records import Record, UnitStatus, lanes_as_dict
from behave_campaign.domain.state import (
    busy_by_pool,
    count_states,
    group_by_scenario,
    lifecycle,
    reduce_units,
    running,
)
from behave_campaign.messages import (
    AttemptView,
    CampaignHeader,
    CampaignListing,
    CampaignRepo,
    CampaignScope,
    CampaignState,
    ProblemUnit,
    ScenarioProblems,
    ScopeSize,
    StateCounts,
    UnitHistoryView,
    UnitView,
)


def header_of(records: Sequence[Record]) -> HeaderRecord | None:
    for record in records:
        if isinstance(record, HeaderRecord):
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
    header = header_of(records) or HeaderRecord()
    return CampaignHeader(
        campaign_id=campaign_id,
        created_at=header.at,
        install_from=header.install_from,
        lanes=lanes_as_dict(header.lanes),
        total_units=len(reduce_units(records)),
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
    statuses = reduce_units(records)
    return CampaignState(
        campaign_id=campaign_id,
        lifecycle=lifecycle(records),
        lanes_busy=len(running(statuses)),
        counts=StateCounts(
            **count_states(statuses if counted is None else counted)
        ),
    )


def lanes_busy_by_pool(records: Sequence[Record]) -> dict[str, int]:
    """Lanes in flight for each pool the campaign has lanes in."""
    header = header_of(records) or HeaderRecord()
    busy = busy_by_pool(reduce_units(records))
    return {pool.value: busy.get(pool, 0) for pool in header.lanes}


def campaign_listing(
    campaign_id: str, records: Sequence[Record]
) -> CampaignListing:
    header = header_of(records) or HeaderRecord()
    scope = header.filters
    return CampaignListing(
        **campaign_state(campaign_id, records).model_dump(),
        created_at=header.at,
        install_from=header.install_from,
        lanes=lanes_as_dict(header.lanes),
        total_units=len(reduce_units(records)),
        scope_size=ScopeSize(
            features=len(scope.feature),
            scenarios=len(scope.scenario),
            releases=len(scope.release),
            machine_types=len(scope.machine_type),
        ),
    )


def scenario_problems(
    statuses: Sequence[UnitStatus],
) -> list[ScenarioProblems]:
    rows = []
    for (feature, scenario), units in group_by_scenario(statuses):
        row = ScenarioProblems(feature=feature, scenario=scenario)
        for status in units:
            getattr(row, str(status.state)).append(
                ProblemUnit(
                    release=status.unit.release,
                    machine_type=status.unit.machine_type,
                    job_id=status.job_id,
                    attempt_count=len(status.attempts),
                )
            )
        rows.append(row)
    return rows
