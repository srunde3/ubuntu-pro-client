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
from typing import Any, Callable, Mapping, Sequence

from behave_campaign.domain.bundle import (
    AUDIT_CAMPAIGN_NAME,
    AUDIT_DIR,
    AUDIT_EVENTS_NAME,
    CAMPAIGN_JSON_NAME,
    JOB_FILES,
    JOBS_DIR,
    SUMMARY_NAME,
    UNITS_CSV_NAME,
    BundleEntry,
    job_ids,
)
from behave_campaign.domain.classify import finished_from_mcp_payload
from behave_campaign.domain.events import (
    MAX_EVENT_ERROR_CHARS,
    EventKind,
    NewEvent,
    expand_event_kinds,
    shorten_failures,
)
from behave_campaign.domain.records import (
    DEFAULT_INSTALL_SOURCE,
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    Filters,
    Lifecycle,
    LifecycleRecord,
    PlanRecord,
    Record,
    RepoState,
    UnitState,
    UnitStatus,
    lanes_as_dict,
    parse_finished,
    resolve_lanes,
    validate_campaign_id,
    validate_install_source,
)
from behave_campaign.domain.state import (
    GROUPINGS,
    GroupBy,
    count_states,
    describe_units,
    lifecycle,
    outstanding,
    problems,
    reduce_units,
    running,
    select_next,
)
from behave_campaign.messages import (
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_PROBLEMS_LIMIT,
    DEFAULT_UNITS_LIMIT,
    MAX_EVENTS_LIMIT,
    MAX_UNITS_LIMIT,
    AwaitEventsResponse,
    CampaignState,
    CampaignStatusResponse,
    CreateCampaignResponse,
    DimensionsResponse,
    DimensionValue,
    EventView,
    ExportCampaignResponse,
    ListCampaignsResponse,
    NextUnitsResponse,
    RecordAttemptsResponse,
    UnitHistoryResponse,
    UnitsResponse,
)
from behave_campaign.ports import (
    BundleWriter,
    CampaignStore,
    EventLog,
    FeatureReader,
    JobFiles,
)
from behave_campaign.views.export import (
    render_campaign_json,
    render_summary,
    render_units_csv,
)
from behave_campaign.views.responses import (
    campaign_header,
    campaign_listing,
    campaign_state,
    header_of,
    lanes_busy_by_pool,
    scenario_problems,
    unit_history_view,
    unit_view,
)
from behave_mcp.domain.pools import Pool


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
        lane_ceiling: Mapping[Pool, int] | None = None,
    ) -> None:
        """``lane_ceiling`` caps a campaign's lanes in each pool.

        The MCP passes its concurrent-job limit. ``None`` means no ceiling
        applies, which is the CLI's case: it runs nothing itself, so the
        limit is enforced wherever a runner has to honour those lanes.
        """
        self._store = store
        self._features = features
        self._events = events
        self._now = now
        self._repo_state = repo_state
        self._lane_ceiling = lane_ceiling

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
        install_from: str = DEFAULT_INSTALL_SOURCE,
        lanes: Mapping[Pool, int] | None = None,
    ) -> CreateCampaignResponse:
        """Plan a campaign and store it. Starts nothing.

        Every campaign records the install source its jobs run with and how
        many lanes a runner may fill, because a campaign that does not say
        is underspecified: one created from the CLI can still be handed to a
        runner later.
        """
        validate_campaign_id(campaign_id)
        validate_install_source(install_from)

        filters = Filters(
            feature=tuple(feature_files),
            scenario=tuple(scenarios),
            release=tuple(releases),
            machine_type=tuple(machine_types),
        )
        units = self._features.discover_units(repo_root, filters)
        if not units:
            raise CampaignError("no test units matched the requested filters")
        resolved_lanes = resolve_lanes(units, lanes or {})
        self._check_lane_ceiling(resolved_lanes)

        at = self._now()
        header = CampaignHeader(
            at=at,
            campaign_id=campaign_id,
            repo=self._repo_state(repo_root),
            filters=filters,
            install_from=install_from,
            lanes=resolved_lanes,
        )
        plans = [PlanRecord(unit=unit, at=at) for unit in units]
        self._store.create(campaign_id, header, plans)
        self._events.append(
            campaign_id,
            [
                NewEvent(
                    kind=EventKind.CAMPAIGN_CREATED,
                    at=at,
                    data={
                        "total_units": len(units),
                        "install_from": install_from,
                        "lanes": lanes_as_dict(resolved_lanes),
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
        validate_install_source(install_from)
        if from_mcp:
            parsed = finished_from_mcp_payload(payload)
        else:
            if not isinstance(payload, list) or not payload:
                raise CampaignError("input must be a non-empty JSON array")
            parsed = [parse_finished(raw) for raw in payload]

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
        statuses = reduce_units(settled)
        return RecordAttemptsResponse(
            **campaign_state(campaign_id, settled).model_dump(),
            recorded=len(parsed),
            running=[unit_view(status) for status in running(statuses)],
            problems=[unit_view(status) for status in problems(statuses)],
        )

    def complete_campaign(self, *, campaign_id: str) -> CampaignState:
        """Mark a campaign complete once it has no work left.

        This is the transition for a campaign recorded by hand; a server
        writes it on its own when the last lane finishes.
        """
        records = self._store.replay(campaign_id)
        state = lifecycle(records)
        if state == Lifecycle.COMPLETE:
            return campaign_state(campaign_id, records)
        if state == Lifecycle.CANCELLED:
            raise CampaignError(
                "campaign {!r} was cancelled; reopen it instead".format(
                    campaign_id
                )
            )
        left = outstanding(records)
        if left:
            raise CampaignError(
                "campaign {!r} has {} unit(s) left to run or retry: {}".format(
                    campaign_id,
                    len(left),
                    describe_units(status.unit for status in left),
                )
            )

        at = self._now()
        record = LifecycleRecord(state=Lifecycle.COMPLETE, at=at)
        self._store.append(campaign_id, [record])
        settled = [*records, record]
        self._events.append(
            campaign_id,
            [
                NewEvent(
                    kind=EventKind.CAMPAIGN_COMPLETE,
                    at=at,
                    data={"counts": count_states(reduce_units(settled))},
                )
            ],
        )
        return campaign_state(campaign_id, settled)

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
        group_by: str = GroupBy.UNIT,
        problems_limit: int = DEFAULT_PROBLEMS_LIMIT,
    ) -> CampaignStatusResponse:
        """Report counts, the units in flight, and the units needing action.

        ``filters`` narrows which units are counted and listed, so a caller
        can ask about one release without reading the whole campaign.
        ``group_by`` chooses how the problems read: one unit per row, or one
        scenario per row with its units bucketed by state. Individual units
        are :meth:`list_units`' job.
        """
        if group_by not in GROUPINGS:
            raise CampaignError(
                "unknown group_by {!r}; choose one of {}".format(
                    group_by, ", ".join(GROUPINGS)
                )
            )
        records = self._store.replay(campaign_id)
        statuses = [
            status
            for status in reduce_units(records)
            if filters.matches(status)
        ]

        problem_units = problems(statuses)
        problems_cap, _ = self._cap(problems_limit)

        return CampaignStatusResponse(
            campaign=campaign_header(campaign_id, records),
            state=campaign_state(campaign_id, records, statuses),
            lanes_busy_by_pool=lanes_busy_by_pool(records),
            running=[unit_view(status) for status in running(statuses)],
            problems=(
                [unit_view(status) for status in problem_units[:problems_cap]]
                if group_by == GroupBy.UNIT
                else None
            ),
            problem_scenarios=(
                scenario_problems(problem_units)[:problems_cap]
                if group_by == GroupBy.SCENARIO
                else None
            ),
            problems_total=len(problem_units),
        )

    def list_units(
        self,
        *,
        campaign_id: str,
        filters: Filters = Filters(),
        limit: int = DEFAULT_UNITS_LIMIT,
    ) -> UnitsResponse:
        """Report every selected unit with its current state."""
        statuses = self._selected(campaign_id, filters)
        capped, clamped = self._cap(limit)
        return UnitsResponse(
            units=[unit_view(status) for status in statuses[:capped]],
            truncated=len(statuses) > capped,
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
                for status in select_next(
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
        failure_chars: int = MAX_EVENT_ERROR_CHARS,
    ) -> AwaitEventsResponse:
        """Return events after ``since_seq``, waiting up to ``timeout``.

        ``kinds`` subscribes by exact kind or by family (``unit.*``); empty
        means everything. A timeout returns an empty batch rather than an
        error, and the campaign summary comes back either way, so a quiet
        stretch still tells the caller where things stand. ``failure_chars``
        shortens the error messages a ``unit.failed`` event carries; the
        stored event keeps them whole.
        """
        if failure_chars < 0:
            raise CampaignError("failure_chars must not be negative")
        patterns = expand_event_kinds(kinds)
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
                    data=shorten_failures(event.data, failure_chars),
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

    # -- exporting --------------------------------------------------------

    def export_campaign(
        self,
        *,
        campaign_id: str,
        output: Path,
        jobs: JobFiles,
        writer: BundleWriter,
    ) -> ExportCampaignResponse:
        """Write a complete campaign's evidence bundle to ``output``.

        The bundle holds SUMMARY.md, campaign.json and units.csv for a
        reviewer, every attempt's job files under ``jobs/``, and the raw
        campaign record and event log under ``audit/``. Only a complete
        campaign exports: evidence is a finished record, never a snapshot.
        """
        records = self._store.replay(campaign_id)
        current = lifecycle(records)
        statuses = reduce_units(records)
        if current != Lifecycle.COMPLETE:
            counts = count_states(statuses)
            raise CampaignError(
                "campaign {!r} is {} with {} running and {} unattempted "
                "units; only a complete campaign can be exported".format(
                    campaign_id,
                    current,
                    counts[UnitState.RUNNING],
                    counts[UnitState.UNATTEMPTED],
                )
            )
        header = header_of(records) or CampaignHeader()

        entries: list[BundleEntry] = []
        missing: list[str] = []
        missing_jobs: set[str] = set()
        all_jobs = job_ids(statuses)
        for job_id in all_jobs:
            found = jobs.files(job_id)
            for name in JOB_FILES:
                bundled = "{}/{}/{}".format(JOBS_DIR, job_id, name)
                if name in found:
                    entries.append(
                        BundleEntry(name=bundled, source=found[name])
                    )
                else:
                    missing.append(bundled)
                    missing_jobs.add(job_id)

        audit = [
            (AUDIT_CAMPAIGN_NAME, self._store.path_for(campaign_id)),
            (AUDIT_EVENTS_NAME, self._events.path_for(campaign_id)),
        ]
        for name, source in audit:
            if source is not None:
                entries.append(
                    BundleEntry(
                        name="{}/{}".format(AUDIT_DIR, name),
                        source=source,
                    )
                )

        entries += [
            BundleEntry(
                name=SUMMARY_NAME,
                data=render_summary(
                    campaign_id, header, statuses, missing_jobs
                ).encode(),
            ),
            BundleEntry(
                name=CAMPAIGN_JSON_NAME,
                data=render_campaign_json(
                    campaign_id, header, current, statuses
                ).encode(),
            ),
            BundleEntry(
                name=UNITS_CSV_NAME,
                data=render_units_csv(statuses).encode(),
            ),
        ]
        skipped = writer.write(output, campaign_id, entries)

        return ExportCampaignResponse(
            campaign_id=campaign_id,
            path=str(output),
            counts=campaign_state(campaign_id, records).counts,
            jobs=len(all_jobs),
            # A campaign created outside a server has no event log.
            missing_files=sorted(
                [
                    *missing,
                    *(
                        name
                        for name in skipped
                        if not name.startswith(AUDIT_DIR + "/")
                    ),
                ]
            ),
        )

    # -- internals --------------------------------------------------------

    def _selected(
        self, campaign_id: str, filters: Filters
    ) -> list[UnitStatus]:
        return [
            status
            for status in reduce_units(self._store.replay(campaign_id))
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
                    len(unplanned), describe_units(unplanned)
                )
            )

    def _check_lane_ceiling(self, lanes: Mapping[Pool, int]) -> None:
        if self._lane_ceiling is None:
            return
        for pool, count in lanes.items():
            limit = self._lane_ceiling.get(pool, 0)
            if not limit:
                raise CampaignError(
                    "the campaign has {pool} units, but the server has "
                    "disabled the {pool} pool".format(pool=pool)
                )
            if count > limit:
                raise CampaignError(
                    "{} {} lanes exceed the server's {} job limit of {}. "
                    "Such a campaign would run slower than asked and look "
                    "like a hang, so raise the server's limit or lower the "
                    "lanes.".format(count, pool, pool, limit)
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
