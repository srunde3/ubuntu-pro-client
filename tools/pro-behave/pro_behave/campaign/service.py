"""Orchestrates the campaign domain and ports into tool behaviours.

``CampaignService`` is driven by the MCP tool handlers in
``pro_behave.mcp.server`` and by ``pro_behave.cli``. Neither the clock nor
the filesystem is reached directly, so the whole service is exercisable with
fakes.

Every method takes keyword arguments only and addresses a campaign by its id,
so a call reads the same wherever it comes from.
"""

from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, ContextManager, Mapping, Sequence

from pro_behave.campaign.domain.bundle import (
    AUDIT_DIR,
    AUDIT_FILES,
    EXPORT_JSON_NAME,
    JOB_FILES,
    JOBS_DIR,
    SUMMARY_NAME,
    UNITS_CSV_NAME,
    BundleEntry,
    job_ids,
)
from pro_behave.campaign.domain.classify import KILLED
from pro_behave.campaign.domain.events import (
    MAX_EVENT_ERROR_CHARS,
    event_matches,
    expand_event_kinds,
    shorten_failures,
)
from pro_behave.campaign.domain.feed import derive_feed
from pro_behave.campaign.domain.records import (
    DEFAULT_INSTALL_FROM,
    PROBLEM_OUTCOMES,
    CampaignError,
    CampaignRecord,
    CampaignState,
    Cause,
    Filters,
    Intent,
    IntentRecord,
    InterruptedRecord,
    PlanRecord,
    Record,
    RepoState,
    RetryRecord,
    Scope,
    StartedRecord,
    StarvedRecord,
    Unit,
    UnitHistory,
    resolve_max_jobs,
    validate_campaign_id,
    validate_install_from,
)
from pro_behave.campaign.domain.schedule import InFlightJob, plan_campaign
from pro_behave.campaign.domain.signals import (
    DEFAULT_OVERDUE_SECONDS,
    overdue_jobs,
)
from pro_behave.campaign.domain.state import (
    GROUPINGS,
    GroupBy,
    in_flight,
    intent,
    problems,
    reduce_units,
    replay,
    state_of,
)
from pro_behave.campaign.messages import (
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_PROBLEMS_LIMIT,
    DEFAULT_UNITS_LIMIT,
    MAX_EVENTS_LIMIT,
    MAX_UNITS_LIMIT,
    AwaitEventsResponse,
    CampaignControlResponse,
    CampaignStatusResponse,
    CampaignSummary,
    CreateCampaignResponse,
    EventView,
    ExportCampaignResponse,
    ListCampaignsResponse,
    RetryUnitsResponse,
    UnitHistoryResponse,
    UnitsResponse,
)
from pro_behave.campaign.ports import (
    BundleWriter,
    CampaignStore,
    FeatureReader,
    JobFiles,
    JobRunner,
    QueueRefused,
)
from pro_behave.campaign.views.export import (
    render_campaign_json,
    render_summary,
    render_units_csv,
)
from pro_behave.campaign.views.responses import (
    campaign_header,
    campaign_listing,
    campaign_summary,
    header_of,
    in_flight_view,
    scenario_problems,
    unit_history_view,
    unit_view,
)
from pro_behave.jobs.domain.pools import Pool

logger = logging.getLogger(__name__)

# States with nothing to queue and, once nothing is in flight, nothing to
# record: reconcile skips such a campaign after reading it.
SETTLED_STATES = (
    CampaignState.CREATED,
    CampaignState.PAUSED,
    CampaignState.COMPLETE,
)


class CampaignService:
    """Create, run, pause and retry campaigns, reconcile them, and report.

    Nothing about a campaign is held in memory: every method reads its
    records, and writes records only. Commands that write, and reconcile,
    hold the state directory's one lock, so they take turns with each other
    and with every other process on the checkout.
    """

    def __init__(
        self,
        *,
        store: CampaignStore,
        features: FeatureReader,
        now: Callable[[], str],
        repo_state: Callable[[Path], RepoState],
        max_jobs_ceiling: Mapping[Pool, int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        overdue_seconds: float = DEFAULT_OVERDUE_SECONDS,
        jobs: JobRunner | None = None,
    ) -> None:
        """``max_jobs_ceiling`` caps a campaign's max jobs in each pool: the
        checkout's own. ``None`` means no ceiling applies.

        ``jobs`` is the job service; without it, the commands that queue or
        kill jobs are refused, and creating a campaign takes no lock.
        """
        self._store = store
        self._features = features
        self._jobs = jobs
        self._now = now
        self._repo_state = repo_state
        self._max_jobs_ceiling = max_jobs_ceiling
        self._monotonic = monotonic
        self._overdue_seconds = overdue_seconds

    # -- creating ---------------------------------------------------------

    def create_campaign(
        self,
        *,
        campaign_id: str,
        repo_root: Path,
        releases: Sequence[str] = (),
        machine_types: Sequence[str] = (),
        features: Sequence[str] = (),
        scenarios: Sequence[str] = (),
        install_from: str = DEFAULT_INSTALL_FROM,
        max_jobs: Mapping[Pool, int] | None = None,
    ) -> CreateCampaignResponse:
        """Plan a campaign and store it. Starts nothing.

        Every campaign records the install source its jobs run with and how
        many of its jobs may be in flight in each pool, because a campaign
        that does not say is underspecified.
        """
        validate_campaign_id(campaign_id)
        validate_install_from(install_from)

        scope = Scope(
            feature=tuple(features),
            scenario=tuple(scenarios),
            release=tuple(releases),
            machine_type=tuple(machine_types),
        )
        units = self._features.discover_units(repo_root, scope)
        if not units:
            raise CampaignError("no test units matched the requested filters")
        resolved = resolve_max_jobs(units, max_jobs or {})
        self._check_max_jobs_ceiling(resolved)

        at = self._now()
        header = CampaignRecord(
            at=at,
            campaign_id=campaign_id,
            repo=self._repo_state(repo_root),
            scope=scope,
            install_from=install_from,
            max_jobs=resolved,
        )
        plans = [PlanRecord(unit=unit, at=at) for unit in units]
        with self._lock():
            self._store.create(campaign_id, header, plans)

        records: list[Record] = [header, *plans]
        return CreateCampaignResponse(
            campaign=campaign_header(campaign_id, records),
            summary=campaign_summary(campaign_id, records),
        )

    # -- commands ---------------------------------------------------------

    def run(self, *, campaign_id: str) -> CampaignControlResponse:
        """Set intent to run, and queue what can be queued."""
        with self._runner().lock():
            records = self._store.read_records(campaign_id)
            self._store.append(
                campaign_id,
                self._intent_change(records, Intent.RUN, Cause.RUN),
            )
            self._reconcile_campaign(campaign_id)
            self._runner().reconcile_jobs()
        return self._control_response(campaign_id)

    def pause(
        self, *, campaign_id: str, kill: bool = False
    ) -> CampaignControlResponse:
        """Set intent to hold. Jobs in flight finish, or are killed.

        A killed job's attempt is recorded as interrupted at once, so the
        campaign is paused at once and its unit queueable again.
        """
        with self._runner().lock():
            records = self._store.read_records(campaign_id)
            self._store.append(
                campaign_id,
                self._intent_change(records, Intent.HOLD, Cause.PAUSE),
            )
            killed: list[UnitHistory] = []
            not_killed: list[str] = []
            if kill:
                killed = in_flight(reduce_units(records))
                not_killed = self._kill(campaign_id, killed)
                at = self._now()
                self._store.append(
                    campaign_id,
                    [
                        InterruptedRecord(
                            unit=status.unit,
                            job_id=status.job_id or "",
                            at=at,
                            reason=KILLED,
                        )
                        for status in killed
                    ],
                )
            response = self._control_response(campaign_id)
        response.killed = [unit_view(status) for status in killed]
        response.not_killed = not_killed
        return response

    def retry_units(
        self,
        *,
        campaign_id: str,
        filters: Filters = Filters(),
        reason: str = "",
    ) -> RetryUnitsResponse:
        """Ask for another attempt at units that already had one, and run.

        With no state filter this selects the problem states -- failed,
        skipped and errored -- because those are what a rerun is usually
        for. Name a state explicitly to retry something else, including a
        unit that passed. Units in flight are never selected; they are
        already being attempted.
        """
        with self._runner().lock():
            records = self._store.read_records(campaign_id)
            wanted = filters.state or PROBLEM_OUTCOMES
            selected = [
                status
                for status in reduce_units(records)
                if not status.in_flight
                and status.state in wanted
                and filters.matches_unit(status.unit)
            ]
            if not selected:
                raise CampaignError("no units matched; nothing was re-queued")
            at = self._now()
            # The intent comes first, so the retries are what move the
            # campaign, and its state changes once.
            self._store.append(
                campaign_id,
                [
                    *self._intent_change(records, Intent.RUN, Cause.RETRY),
                    *(
                        RetryRecord(unit=status.unit, at=at, reason=reason)
                        for status in selected
                    ),
                ],
            )
            self._reconcile_campaign(campaign_id)
            self._runner().reconcile_jobs()
            return RetryUnitsResponse(
                **self._summary(campaign_id).model_dump(),
                retried=len(selected),
                units=[unit_view(status) for status in selected],
            )

    # -- reconciling ------------------------------------------------------

    def reconcile(self) -> None:
        """Bring every campaign up to date, then start queued jobs.

        For each campaign: record its ended jobs, queue its queueable units
        under intent run, and note what a watcher should hear. Any process
        may call this at any time; it decides from files alone.
        """
        with self._runner().lock():
            for campaign_id in self._store.list_ids():
                try:
                    self._reconcile_campaign(campaign_id)
                except CampaignError:
                    # One unreadable campaign must not hold up the others.
                    logger.exception(
                        "campaign %s: reconcile failed", campaign_id
                    )
            self._runner().reconcile_jobs()

    def _reconcile_campaign(self, campaign_id: str) -> None:
        records = self._store.read_records(campaign_id)
        replayed = replay(records)
        if not replayed.in_flight and replayed.state() in SETTLED_STATES:
            return
        header = header_of(records)
        if header is None:
            raise CampaignError(
                "campaign {} has no campaign record".format(campaign_id)
            )
        jobs = self._in_flight_jobs(records)
        at = self._now()
        plan = plan_campaign(
            records=records, jobs=jobs, max_jobs=header.max_jobs, at=at
        )
        self._store.append(campaign_id, plan.record)
        queued, refused = self._queue(campaign_id, header, plan.queue)
        replayed = replay([*records, *plan.record, *queued])
        observed: list[Record] = [
            refusal
            for refusal in refused
            if refusal.pool not in replayed.starved_pools
        ]
        observed += overdue_jobs(
            [job for job in jobs if not job.ended],
            at=at,
            overdue_seconds=self._overdue_seconds,
            reported=replayed.overdue_jobs,
        )
        self._store.append(campaign_id, observed)

    def _in_flight_jobs(self, records: Sequence[Record]) -> list[InFlightJob]:
        jobs = []
        for status in reduce_units(records):
            if not status.in_flight or not status.job_id:
                continue
            jobs.append(
                InFlightJob(
                    unit=status.unit,
                    job_id=status.job_id,
                    result=self._runner().poll(status.job_id),
                    # What this job actually runs with.
                    install_from=status.attempts[-1].install_from,
                )
            )
        return jobs

    def _queue(
        self,
        campaign_id: str,
        header: CampaignRecord,
        units: Sequence[Unit],
    ) -> tuple[list[StartedRecord], list[StarvedRecord]]:
        """Queue a job for each unit; return the attempts recorded and the
        refusals, one per pool."""
        repo_root = Path(header.repo.root)
        queued: list[StartedRecord] = []
        refused: dict[Pool, StarvedRecord] = {}
        for unit in units:
            if unit.pool in refused:
                continue
            started = StartedRecord(
                unit=unit,
                job_id=self._runner().new_job_id(),
                install_from=header.install_from,
                at=self._now(),
            )
            try:
                self._runner().queue(
                    started.job_id,
                    unit,
                    repo_root=repo_root,
                    install_from=header.install_from,
                    campaign_id=campaign_id,
                    # The attempt is recorded before its job exists, so a
                    # crash leaves an attempt to close, never a job nobody
                    # records.
                    record=lambda: self._store.append(campaign_id, [started]),
                )
            except QueueRefused as error:
                refused[unit.pool] = StarvedRecord(
                    unit=unit,
                    pool=unit.pool,
                    reason=str(error),
                    at=self._now(),
                )
                continue
            queued.append(started)
        return queued, list(refused.values())

    def _kill(
        self, campaign_id: str, statuses: Sequence[UnitHistory]
    ) -> list[str]:
        """Kill each job; return the ids of those that could not be."""
        not_killed = []
        for status in statuses:
            job_id = status.job_id or ""
            if not self._runner().kill(job_id):
                logger.warning(
                    "campaign %s: job %s could not be killed",
                    campaign_id,
                    job_id,
                )
                not_killed.append(job_id)
        return not_killed

    def _runner(self) -> JobRunner:
        if self._jobs is None:
            raise CampaignError(
                "this command needs the checkout's job service: name the "
                "checkout with --repo-root or UBUNTU_PRO_CLIENT_REPO"
            )
        return self._jobs

    def _lock(self) -> ContextManager[None]:
        return nullcontext() if self._jobs is None else self._jobs.lock()

    def _intent_change(
        self, records: Sequence[Record], wanted: Intent, cause: Cause
    ) -> list[Record]:
        """The record that sets ``wanted``, or none if it is already set."""
        if intent(records) == wanted:
            return []
        return [IntentRecord(intent=wanted, cause=cause, at=self._now())]

    def _summary(self, campaign_id: str) -> CampaignSummary:
        return campaign_summary(
            campaign_id, self._store.read_records(campaign_id)
        )

    def _control_response(self, campaign_id: str) -> CampaignControlResponse:
        return CampaignControlResponse(
            **self._summary(campaign_id).model_dump()
        )

    # -- reporting --------------------------------------------------------

    def list_campaigns(self) -> ListCampaignsResponse:
        """Summarise every stored campaign."""
        return ListCampaignsResponse(
            campaign_dir=str(self._store.root),
            campaigns=[
                campaign_listing(
                    campaign_id, self._store.read_records(campaign_id)
                )
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
        records = self._store.read_records(campaign_id)
        statuses = [
            status
            for status in reduce_units(records)
            if filters.matches(status)
        ]

        problem_units = problems(statuses)
        problems_cap, _ = self._cap(problems_limit)

        return CampaignStatusResponse(
            campaign=campaign_header(campaign_id, records),
            summary=campaign_summary(campaign_id, records, statuses),
            in_flight_by_pool=in_flight_view(records),
            in_flight=[unit_view(status) for status in in_flight(statuses)],
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
        recorded failure keeps them whole.

        The events are derived from the campaign's records on every read,
        so ``seq`` numbers the same events the same way across restarts.
        """
        if failure_chars < 0:
            raise CampaignError("failure_chars must not be negative")
        patterns = expand_event_kinds(kinds)
        capped, _ = self._cap_events(limit)

        deadline = self._monotonic() + timeout
        while True:
            records = self._store.read_records(campaign_id)
            feed = derive_feed(campaign_id, records)
            events = [
                event
                for event in feed
                if event.seq > since_seq
                and event_matches(event.kind, patterns)
            ][:capped]
            remaining = deadline - self._monotonic()
            if events or remaining <= 0:
                break
            self._store.wait(campaign_id, len(records), remaining)

        return AwaitEventsResponse(
            **campaign_summary(campaign_id, records).model_dump(),
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
            latest_seq=len(feed),
            timed_out=not events,
        )

    def export_campaign(
        self,
        *,
        campaign_id: str,
        output: Path,
        jobs: JobFiles,
        writer: BundleWriter,
    ) -> ExportCampaignResponse:
        """Write a complete campaign's evidence bundle to ``output``.

        The bundle holds SUMMARY.md, export.json and units.csv for a
        reviewer, every attempt's job files under ``jobs/``, and the raw
        campaign files under ``audit/``. Only a complete
        campaign exports: evidence is a finished record, never a snapshot.
        """
        records = self._store.read_records(campaign_id)
        current = state_of(records)
        statuses = reduce_units(records)
        if current != CampaignState.COMPLETE:
            busy = in_flight(statuses)
            raise CampaignError(
                "campaign {!r} is {} with {} unit(s) in flight{} and {} "
                "queueable; only a complete campaign can be exported".format(
                    campaign_id,
                    current,
                    len(busy),
                    (
                        " (jobs {})".format(
                            ", ".join(str(s.job_id) for s in busy)
                        )
                        if busy
                        else ""
                    ),
                    sum(1 for status in statuses if status.queueable),
                )
            )
        header = header_of(records) or CampaignRecord()

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

        # The campaign's own files, as stored, for whoever audits it.
        campaign_dir = self._store.path_for(campaign_id)
        entries += [
            BundleEntry(
                name="{}/{}".format(AUDIT_DIR, name),
                source=campaign_dir / name,
            )
            for name in AUDIT_FILES
        ]

        entries += [
            BundleEntry(
                name=SUMMARY_NAME,
                data=render_summary(
                    campaign_id, header, statuses, missing_jobs
                ).encode(),
            ),
            BundleEntry(
                name=EXPORT_JSON_NAME,
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
            counts=campaign_summary(campaign_id, records).counts,
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
    ) -> list[UnitHistory]:
        return [
            status
            for status in reduce_units(self._store.read_records(campaign_id))
            if filters.matches(status)
        ]

    def _check_max_jobs_ceiling(self, max_jobs: Mapping[Pool, int]) -> None:
        if self._max_jobs_ceiling is None:
            return
        for pool, count in max_jobs.items():
            limit = self._max_jobs_ceiling.get(pool, 0)
            if not limit:
                raise CampaignError(
                    "the campaign has {pool} units, but the {pool} pool is "
                    "disabled for this checkout".format(pool=pool)
                )
            if count > limit:
                raise CampaignError(
                    "{count} {pool} max jobs exceed the checkout's "
                    "max_jobs.{pool} of {limit}. Such a campaign would run "
                    "slower than asked and look like a hang, so raise the "
                    "checkout's max_jobs.{pool} or ask for fewer.".format(
                        count=count, pool=pool, limit=limit
                    )
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
