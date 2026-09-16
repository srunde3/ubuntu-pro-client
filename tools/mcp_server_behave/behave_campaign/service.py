"""Orchestrates the campaign domain and ports into tool behaviours.

``CampaignService`` is driven by the MCP tool wrappers in
``behave_mcp.server`` and by ``behave_campaign.cli``. Neither the clock nor
the filesystem is reached directly, so the whole service is exercisable with
fakes.

Every method takes keyword arguments only and addresses a campaign by its id,
so a call reads the same wherever it comes from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from behave_campaign import domain
from behave_campaign.domain import (
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    Filters,
    PlanRecord,
    Record,
    RepoState,
    UnitStatus,
)
from behave_campaign.messages import (
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_UNITS_LIMIT,
    MAX_EVENTS_LIMIT,
    MAX_UNITS_LIMIT,
    AwaitEventsResponse,
    CampaignStatusResponse,
    CreateCampaignResponse,
    DimensionsResponse,
    DimensionValue,
    EventView,
    ListCampaignsResponse,
    NextUnitsResponse,
    RecordAttemptsResponse,
    UnitHistoryResponse,
)
from behave_campaign.ports import CampaignStore, EventLog, FeatureReader
from behave_campaign.views import (
    campaign_header,
    campaign_listing,
    campaign_state,
    unit_history_view,
    unit_view,
)


class CampaignService:
    """Create campaigns, record what happened, and report on them."""

    def __init__(
        self,
        *,
        store: CampaignStore,
        features: FeatureReader,
        events: EventLog,
        now: Callable[[], str],
        repo_state: Callable[[Path], RepoState],
        max_lane_ceiling: int | None = None,
    ) -> None:
        """``max_lane_ceiling`` caps a campaign's ``max_lanes``.

        The MCP passes its concurrent-job limit. ``None`` means no ceiling
        applies, which is the CLI's case: it runs nothing itself, so the
        limit is enforced wherever a runner has to honour those lanes.
        """
        self._store = store
        self._features = features
        self._events = events
        self._now = now
        self._repo_state = repo_state
        self._max_lane_ceiling = max_lane_ceiling

    # -- creating ---------------------------------------------------------

    def create_campaign(
        self,
        *,
        campaign_id: str,
        repo_root: Path,
        releases: Sequence[str] = (),
        machine_types: Sequence[str] = (),
        feature_files: Sequence[str] = (),
        scenarios: Sequence[str] = (),
        install_from: str = domain.DEFAULT_INSTALL_SOURCE,
        max_lanes: int = 1,
    ) -> CreateCampaignResponse:
        """Plan a campaign and store it. Starts nothing.

        Every campaign records the install source its jobs run with and how
        many lanes a runner may fill, because a campaign that does not say
        is underspecified: one created from the CLI can still be handed to a
        runner later.
        """
        domain.validate_campaign_id(campaign_id)
        domain.validate_install_source(install_from)
        self._validate_max_lanes(max_lanes)

        filters = Filters(
            feature=tuple(feature_files),
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
        self._events.append(
            campaign_id,
            [
                domain.NewEvent(
                    kind=domain.EventKind.CAMPAIGN_CREATED,
                    at=at,
                    data={
                        "total_units": len(units),
                        "install_from": install_from,
                        "max_lanes": max_lanes,
                        "scope": filters.scope_as_dict(),
                    },
                )
            ],
        )

        records: list[Record] = [header, *plans]
        return CreateCampaignResponse(
            campaign=campaign_header(campaign_id, records),
            state=campaign_state(campaign_id, records),
        )

    # -- recording --------------------------------------------------------

    def record_attempts(
        self,
        *,
        campaign_id: str,
        payload: Any,
        install_from: str,
        from_mcp: bool = False,
    ) -> RecordAttemptsResponse:
        """Append attempts to a campaign and report the resulting state.

        Each entry describes one completed try: the unit, its ``job_id`` and
        its ``outcome``. With ``from_mcp``, entries are
        ``{"unit": ..., "result": <completed MCP payload>}`` and the domain
        reads the outcome out of them. An attempt against a unit the campaign
        never planned is rejected, because it means the scope and the work
        have diverged.

        A job still in flight cannot be recorded here: the scheduler owns
        those, having written the start itself.
        """
        domain.validate_install_source(install_from)
        if from_mcp:
            parsed = domain.finished_from_mcp_payload(payload)
        else:
            if not isinstance(payload, list) or not payload:
                raise CampaignError("input must be a non-empty JSON array")
            parsed = [domain.parse_finished(raw) for raw in payload]

        at = self._now()
        # One completed try becomes both halves: it started, and it
        # finished. A caller recording out of band is describing a whole
        # attempt, not half of one.
        records: list[Record] = []
        for finished in parsed:
            records.append(
                AttemptStarted(
                    unit=finished.unit,
                    job_id=finished.job_id,
                    install_from=install_from,
                    at=at,
                )
            )
            records.append(
                AttemptFinished(
                    unit=finished.unit,
                    job_id=finished.job_id,
                    outcome=finished.outcome,
                    at=at,
                )
            )

        existing = self._store.replay(campaign_id)
        self._reject_unplanned(existing, parsed)
        self._store.append(campaign_id, records)

        settled = [*existing, *records]
        statuses = domain.reduce_units(settled)
        return RecordAttemptsResponse(
            **campaign_state(campaign_id, settled).model_dump(),
            recorded=len(parsed),
            running=[unit_view(status) for status in domain.running(statuses)],
            problems=[
                unit_view(status) for status in domain.problems(statuses)
            ],
        )

    # -- reporting --------------------------------------------------------

    def list_campaigns(self) -> ListCampaignsResponse:
        """Summarise every stored campaign."""
        return ListCampaignsResponse(
            campaign_dir=str(self._store.root),
            campaigns=[
                campaign_listing(campaign_id, self._store.replay(campaign_id))
                for campaign_id in self._store.list_ids()
            ],
        )

    def campaign_status(
        self,
        *,
        campaign_id: str,
        filters: Filters = Filters(),
        units_limit: int = 0,
    ) -> CampaignStatusResponse:
        """Report counts, the units in flight, and the units needing action.

        ``filters`` narrows which units are counted and listed, so a caller
        can ask about one release without reading the whole campaign.
        ``units_limit`` is how many individual units to list: the default of
        zero lists none, because a full campaign runs to thousands of units
        and the counts are what a caller usually acts on.
        """
        records = self._store.replay(campaign_id)
        statuses = [
            status
            for status in domain.reduce_units(records)
            if filters.matches(status)
        ]

        units = None
        truncated = False
        clamped = False
        if units_limit:
            capped, clamped = self._cap(units_limit)
            units = [unit_view(status) for status in statuses[:capped]]
            truncated = len(statuses) > capped

        return CampaignStatusResponse(
            campaign=campaign_header(campaign_id, records),
            state=campaign_state(campaign_id, records, statuses),
            running=[unit_view(status) for status in domain.running(statuses)],
            problems=[
                unit_view(status) for status in domain.problems(statuses)
            ],
            units=units,
            truncated=truncated,
            limit_clamped=clamped,
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
        return NextUnitsResponse(
            units=[
                unit_view(status)
                for status in domain.select_next(
                    self._selected(campaign_id, filters), limit
                )
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
        statuses = self._selected(campaign_id, filters)
        capped, clamped = self._cap(limit)
        return UnitHistoryResponse(
            units=[unit_history_view(status) for status in statuses[:capped]],
            truncated=len(statuses) > capped,
            limit_clamped=clamped,
        )

    def await_events(
        self,
        *,
        campaign_id: str,
        since_seq: int = 0,
        kinds: Sequence[str] = (),
        limit: int = DEFAULT_EVENTS_LIMIT,
        timeout: float = 0.0,
    ) -> AwaitEventsResponse:
        """Return events after ``since_seq``, waiting up to ``timeout``.

        ``kinds`` subscribes by exact kind or by family (``unit.*``); empty
        means everything. A timeout returns an empty batch rather than an
        error, and the campaign summary comes back either way, so a quiet
        stretch still tells the caller where things stand.
        """
        patterns = domain.expand_event_kinds(kinds)
        capped, _ = self._cap_events(limit)

        if timeout > 0:
            events = self._events.wait(
                campaign_id,
                since_seq=since_seq,
                kinds=patterns,
                limit=capped,
                timeout=timeout,
            )
        else:
            events = self._events.read(
                campaign_id,
                since_seq=since_seq,
                kinds=patterns,
                limit=capped,
            )

        records = self._store.replay(campaign_id)
        return AwaitEventsResponse(
            **campaign_state(campaign_id, records).model_dump(),
            events=[
                EventView(
                    seq=event.seq,
                    kind=event.kind,
                    at=event.at,
                    data=dict(event.data),
                )
                for event in events
            ],
            next_seq=(events[-1].seq if events else max(since_seq, 0)),
            latest_seq=self._events.latest_seq(campaign_id),
            timed_out=not events,
        )

    def dimensions(self, *, repo_root: Path) -> DimensionsResponse:
        """Report the releases and machine types the feature files can run.

        Reads feature files rather than any campaign, so it is what a caller
        uses to pick a valid scope before creating one.
        """
        raw = self._features.available_dimensions(repo_root)
        return DimensionsResponse(
            releases=[DimensionValue(**value) for value in raw["releases"]],
            machine_types=[
                DimensionValue(**value) for value in raw["machine_types"]
            ],
        )

    # -- internals --------------------------------------------------------

    def _selected(
        self, campaign_id: str, filters: Filters
    ) -> list[UnitStatus]:
        return [
            status
            for status in domain.reduce_units(self._store.replay(campaign_id))
            if filters.matches(status)
        ]

    @staticmethod
    def _reject_unplanned(
        existing: Sequence[Record], finished: Sequence[AttemptFinished]
    ) -> None:
        planned = {
            record.unit
            for record in existing
            if isinstance(record, PlanRecord)
        }
        unplanned = {
            item.unit for item in finished if item.unit not in planned
        }
        if unplanned:
            raise CampaignError(
                "{} attempt(s) reference unplanned units: {}".format(
                    len(unplanned), domain.describe_units(unplanned)
                )
            )

    def _validate_max_lanes(self, max_lanes: int) -> None:
        if max_lanes < 1:
            raise CampaignError("max_lanes must be a positive integer")
        if self._max_lane_ceiling is None:
            return
        if max_lanes > self._max_lane_ceiling:
            raise CampaignError(
                "max_lanes {} exceeds the server's concurrent job limit of "
                "{}. Such a campaign would run serially and look like a "
                "hang, so raise the server's limit or lower "
                "max_lanes.".format(max_lanes, self._max_lane_ceiling)
            )

    @staticmethod
    def _cap_events(limit: int) -> tuple[int, bool]:
        if limit < 1:
            raise CampaignError("limit must be positive")
        if limit > MAX_EVENTS_LIMIT:
            return MAX_EVENTS_LIMIT, True
        return limit, False

    @staticmethod
    def _cap(limit: int) -> tuple[int, bool]:
        if limit < 1:
            raise CampaignError("limit must be positive")
        if limit > MAX_UNITS_LIMIT:
            return MAX_UNITS_LIMIT, True
        return limit, False
