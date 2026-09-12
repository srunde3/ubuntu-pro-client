"""Orchestrates the campaign domain and ports into tool behaviours.

``CampaignService`` is driven by the MCP tool wrappers in
``behave_mcp.server`` and by ``behave_campaign.cli``. Neither the clock nor
the filesystem is reached directly, so the whole service is exercisable with
fakes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from behave_campaign import domain
from behave_campaign.domain import (
    AttemptRecord,
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
    AttemptView,
    CampaignRepo,
    CampaignScope,
    CampaignStatusResponse,
    CampaignSummary,
    CreateCampaignResponse,
    DimensionsResponse,
    DimensionValue,
    ListCampaignsResponse,
    NextUnitsResponse,
    StateCounts,
    UnitHistoryResponse,
    UnitHistoryView,
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


def _unit_history_view(status: UnitStatus) -> UnitHistoryView:
    return UnitHistoryView(
        **_unit_view(status).model_dump(),
        attempts=[
            AttemptView(
                state=attempt.state,
                job_id=attempt.job_id,
                install_from=attempt.install_from,
                at=attempt.at,
            )
            for attempt in status.attempts
        ],
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
        max_parallel_jobs: int | None = None,
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
        install_from: str | None = None,
        max_lanes: int | None = None,
    ) -> CreateCampaignResponse:
        """Plan a campaign and store it. Starts nothing.

        ``install_from`` and ``max_lanes`` are recorded only when given, so a
        campaign created without them stays readable by anything that predates
        those fields.
        """
        domain.validate_campaign_id(campaign_id)
        if install_from is not None:
            domain.validate_install_source(install_from)
        if max_lanes is not None:
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

    def dimensions(self, repo_root: Path) -> DimensionsResponse:
        """Report the releases and machine types the feature files can run."""
        raw = self._features.available_dimensions(repo_root)
        return DimensionsResponse(
            releases=[DimensionValue(**value) for value in raw["releases"]],
            machine_types=[
                DimensionValue(**value) for value in raw["machine_types"]
            ],
        )

    def record_attempts(
        self,
        *,
        campaign_id: str,
        payload: Any,
        install_from: str,
        from_mcp: bool = False,
        filters: Filters = Filters(),
    ) -> CampaignStatusResponse:
        """Append attempts to a campaign and report the resulting state.

        ``payload`` is either a list of attempt objects or, with
        ``from_mcp``, a list of ``{"unit": ..., "result": <MCP payload>}``
        entries that the domain classifies. An attempt against a unit the
        campaign never planned is rejected, because it means the scope and
        the work have diverged.
        """
        domain.validate_install_source(install_from)
        if from_mcp:
            parsed = domain.attempts_from_mcp(payload)
        else:
            if not isinstance(payload, list) or not payload:
                raise CampaignError("input must be a non-empty JSON array")
            parsed = [domain.parse_attempt(raw) for raw in payload]

        at = self._now()
        attempts = [
            AttemptRecord(
                unit=attempt.unit,
                state=attempt.state,
                job_id=attempt.job_id,
                install_from=install_from,
                at=at,
            )
            for attempt in parsed
        ]

        existing = self._store.replay(campaign_id)
        planned = {
            record.unit
            for record in existing
            if isinstance(record, PlanRecord)
        }
        unplanned = {
            attempt.unit for attempt in attempts if attempt.unit not in planned
        }
        if unplanned:
            raise CampaignError(
                "{} attempt(s) reference unplanned units: {}".format(
                    len(unplanned), domain.describe_units(unplanned)
                )
            )

        self._store.append(campaign_id, attempts)
        statuses = [
            status
            for status in domain.reduce_units([*existing, *attempts])
            if filters.matches(status)
        ]
        return CampaignStatusResponse(
            campaign=_summary(campaign_id, _header_of(existing), statuses),
            running=[
                _unit_view(status) for status in domain.running(statuses)
            ],
            problems=[
                _unit_view(status) for status in domain.problems(statuses)
            ],
        )

    def next_units(
        self,
        *,
        campaign_id: str,
        filters: Filters = Filters(),
        limit: int = 1,
    ) -> NextUnitsResponse:
        """Report the units to attempt next.

        Never-attempted units come first, then retryable problems. Units
        that passed, or that are already running, are never returned.
        """
        statuses = [
            status
            for status in domain.reduce_units(self._store.replay(campaign_id))
            if filters.matches(status)
        ]
        return NextUnitsResponse(
            units=[
                _unit_view(status)
                for status in domain.select_next(statuses, limit)
            ]
        )

    def unit_history(
        self,
        *,
        campaign_id: str,
        filters: Filters = Filters(),
        limit: int = DEFAULT_UNITS_LIMIT,
    ) -> UnitHistoryResponse:
        """Report every selected unit with all of its attempts."""
        statuses = [
            status
            for status in domain.reduce_units(self._store.replay(campaign_id))
            if filters.matches(status)
        ]
        capped, clamped = self._cap(limit)
        return UnitHistoryResponse(
            units=[_unit_history_view(status) for status in statuses[:capped]],
            truncated=len(statuses) > capped,
            limit_clamped=clamped,
        )

    def _validate_max_lanes(self, max_lanes: int) -> None:
        if max_lanes < 1:
            raise CampaignError("max_lanes must be a positive integer")
        # The CLI has no server to consult, so it sets no cap; the limit is
        # enforced wherever a server actually has to honour the lanes.
        if self._max_parallel_jobs is None:
            return
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
