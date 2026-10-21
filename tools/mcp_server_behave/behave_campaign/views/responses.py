"""Build the response DTOs from campaign records.

Shared by the service and the runner so that one campaign fact has one
outward shape.
"""

from __future__ import annotations

from typing import Sequence

from behave_campaign.domain.records import (
    CampaignRecord,
    Record,
    UnitHistory,
    max_jobs_as_dict,
)
from behave_campaign.domain.state import (
    campaign_state,
    count_states,
    group_by_scenario,
    in_flight,
    in_flight_by_pool,
    intent,
    progress,
    reduce_units,
)
from behave_campaign.messages import (
    AttemptView,
    CampaignHeader,
    CampaignListing,
    CampaignScope,
    CampaignSummary,
    ProblemUnit,
    RepoState,
    ScenarioProblems,
    ScopeSize,
    StateCounts,
    UnitHistoryView,
    UnitView,
)


def header_of(records: Sequence[Record]) -> CampaignRecord | None:
    for record in records:
        if isinstance(record, CampaignRecord):
            return record
    return None


def unit_view(status: UnitHistory) -> UnitView:
    return UnitView(
        feature=status.unit.feature,
        scenario=status.unit.scenario,
        release=status.unit.release,
        machine_type=status.unit.machine_type,
        state=status.state,
        job_id=status.job_id,
        attempt_count=len(status.attempts),
    )


def unit_history_view(status: UnitHistory) -> UnitHistoryView:
    return UnitHistoryView(
        **unit_view(status).model_dump(),
        attempts=[
            AttemptView(**attempt.as_dict()) for attempt in status.attempts
        ],
    )


def campaign_header(
    campaign_id: str, records: Sequence[Record]
) -> CampaignHeader:
    header = header_of(records) or CampaignRecord()
    return CampaignHeader(
        campaign_id=campaign_id,
        created_at=header.at,
        install_from=header.install_from,
        max_jobs=max_jobs_as_dict(header.max_jobs),
        total_units=len(reduce_units(records)),
        repo=RepoState(**header.repo.as_dict()),
        scope=CampaignScope(**header.scope.as_dict()),
    )


def campaign_summary(
    campaign_id: str,
    records: Sequence[Record],
    counted: Sequence[UnitHistory] | None = None,
) -> CampaignSummary:
    """The campaign's summary; ``counted`` narrows the counts to a selection.

    The state, intent, progress and ``in_flight`` are facts about the
    whole campaign and never narrow.
    """
    statuses = reduce_units(records)
    busy = in_flight(statuses)
    wanted = intent(records)
    left = progress(statuses)
    return CampaignSummary(
        campaign_id=campaign_id,
        state=campaign_state(wanted, left, bool(busy)),
        intent=wanted,
        progress=left,
        in_flight=len(busy),
        counts=StateCounts(
            **count_states(statuses if counted is None else counted)
        ),
    )


def in_flight_view(records: Sequence[Record]) -> dict[str, int]:
    """Jobs in flight for each pool the campaign has max jobs in."""
    header = header_of(records) or CampaignRecord()
    busy = in_flight_by_pool(reduce_units(records))
    return {pool.value: busy.get(pool, 0) for pool in header.max_jobs}


def campaign_listing(
    campaign_id: str, records: Sequence[Record]
) -> CampaignListing:
    header = header_of(records) or CampaignRecord()
    scope = header.scope
    return CampaignListing(
        **campaign_summary(campaign_id, records).model_dump(),
        created_at=header.at,
        install_from=header.install_from,
        max_jobs=max_jobs_as_dict(header.max_jobs),
        total_units=len(reduce_units(records)),
        scope_size=ScopeSize(
            features=len(scope.feature),
            scenarios=len(scope.scenario),
            releases=len(scope.release),
            machine_types=len(scope.machine_type),
        ),
    )


def scenario_problems(
    statuses: Sequence[UnitHistory],
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
