"""``pro-behave``: the command line, which also starts the MCP server.

Commands are grouped by what they act on -- ``job``, ``campaign``,
``feature``, ``scenario``, ``dimension`` and ``config`` -- and each is the
same command as its MCP tool; ``mcp`` serves those tools. Every other
command is a handler: read arguments, call one service method, print the
response as JSON. Behaviour lives in the services, so the CLI and the MCP
tools cannot drift apart. Every handler ends with a reconcile, as every
tool call does.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from pydantic import BaseModel

from pro_behave.campaign.adapters import (
    CampaignDirStore,
    JobDirFiles,
    JsonlCampaignStore,
    ParserFeatureReader,
    ServiceJobRunner,
    TarBundleWriter,
    system_now,
)
from pro_behave.campaign.domain.events import EVENT_FAMILIES, EVENT_PRESETS
from pro_behave.campaign.domain.records import (
    DEFAULT_INSTALL_FROM,
    INSTALL_FROM_VALUES,
    STATES,
    CampaignError,
    CampaignState,
    Filters,
)
from pro_behave.campaign.domain.state import GROUPINGS, GroupBy
from pro_behave.campaign.messages import (
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_UNITS_LIMIT,
    MAX_UNITS_LIMIT,
    AwaitEventsResponse,
)
from pro_behave.campaign.repo import repo_state
from pro_behave.campaign.service import CampaignService
from pro_behave.jobs.adapters import (
    FileConfigStore,
    FileLanePool,
    FileReconcileLock,
    JobProcessLauncher,
    LocalFeatureFileReader,
    LocalJobResultStoreFactory,
    LocalWorkspace,
)
from pro_behave.jobs.domain import layout
from pro_behave.jobs.domain.limits import (
    DEFAULT_FIND_LIMIT,
    DEFAULT_JOB_LIST_LIMIT,
    DEFAULT_LOG_CONTEXT,
    DEFAULT_LOG_LINES,
    DEFAULT_RESULTS_LIMIT,
    DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
)
from pro_behave.jobs.domain.pools import Pool
from pro_behave.jobs.messages import JobState
from pro_behave.jobs.service import (
    JobService,
    JobServiceError,
    UnknownJobError,
)

Output = BaseModel | Iterator[BaseModel]


def _service(
    campaign_dir: Path, args: argparse.Namespace | None = None
) -> CampaignService:
    """The campaign service over one campaign's directory. With the
    checkout known, it can queue and kill jobs, under the checkout's max
    jobs; a campaign directory named outright cannot."""
    jobs = None
    ceiling = None
    if args is not None:
        try:
            service = _job_service(_state_dir(args))
        except ValueError:
            service = None
        if service is not None:
            jobs = _job_runner(service)
            ceiling = service.read_config().max_jobs
    return CampaignService(
        store=CampaignDirStore(campaign_dir),
        features=ParserFeatureReader(),
        now=system_now,
        repo_state=repo_state,
        max_jobs_ceiling=ceiling,
        jobs=jobs,
    )


def _job_runner(service: JobService) -> ServiceJobRunner:
    return ServiceJobRunner(
        service, refusal=JobServiceError, unknown_job=UnknownJobError
    )


def _jobs(args: argparse.Namespace) -> JobService:
    """The job service over this checkout's state directory."""
    return _job_service(_state_dir(args))


def _repo_root(args: argparse.Namespace) -> Path:
    """``--repo-root``, else ``$UBUNTU_PRO_CLIENT_REPO``."""
    given = getattr(args, "repo_root", None) or os.environ.get(
        layout.REPO_ENV_VAR
    )
    if not given:
        raise ValueError(
            "pass --repo-root or set {}".format(layout.REPO_ENV_VAR)
        )
    return Path(given).expanduser().resolve()


def _state_dir(args: argparse.Namespace) -> Path:
    """Where the server keeps its state for this checkout."""
    configured = os.environ.get(layout.STATE_DIR_ENV_VAR)
    return layout.state_dir(
        _repo_root(args),
        Path(configured).expanduser().resolve() if configured else None,
    )


def _campaign_dir(args: argparse.Namespace) -> Path:
    """The campaign's directory: given outright, or the server's for an id.

    The id names the directory the way the server names it, so a human and
    the server read the same record by the same name.
    """
    if args.campaign_dir is not None:
        return args.campaign_dir
    return layout.campaign_paths(
        layout.campaigns_dir(_state_dir(args)), args.campaign_id
    ).campaign_dir


def _campaign_id(args: argparse.Namespace) -> str:
    """The campaign's id: always its directory's name."""
    return _campaign_dir(args).name


def _filters(args: argparse.Namespace) -> Filters:
    return Filters(
        feature=tuple(args.feature or ()),
        scenario=tuple(args.scenario or ()),
        release=tuple(args.release or ()),
        machine_type=tuple(args.machine_type or ()),
        state=tuple(getattr(args, "state", None) or ()),
    )


# -- the catalogue --------------------------------------------------------


def _command_feature_list(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).list_features(
        release=args.release,
        machine_type=args.machine_type,
        tag=args.tag,
        text=args.text,
        repo_root=str(_repo_root(args)),
    )


def _command_feature_describe(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).describe_feature(
        args.feature, repo_root=str(_repo_root(args))
    )


def _command_scenario_find(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).find_scenarios(
        release=args.release,
        machine_type=args.machine_type,
        tag=args.tag,
        text=args.text,
        repo_root=str(_repo_root(args)),
        limit=args.limit,
    )


def _command_dimension_list(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).list_dimensions(repo_root=str(_repo_root(args)))


# -- jobs -----------------------------------------------------------------


def _command_job_run(args: argparse.Namespace) -> BaseModel:
    service = _jobs(args)
    started = service.run_job(
        args.feature,
        args.scenario,
        args.release,
        args.machine_type,
        repo_root=str(_repo_root(args)),
        install_from=args.install_from,
    )
    if not args.wait:
        return started
    return service.wait_for_job(started.job_id, args.max_wait, args.interval)


def _command_job_list(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).list_jobs(limit=args.limit)


def _command_job_results(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).job_results(
        job_ids=args.job_id or None,
        feature=args.feature or "",
        scenario=args.scenario or "",
        release=args.release or "",
        machine_type=args.machine_type or "",
        state=args.state or "",
        limit=args.limit,
    )


def _command_job_wait(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).wait_for_job(args.job_id, args.max_wait, args.interval)


def _command_job_errors(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).job_errors(args.job_id)


def _command_job_logs(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).job_logs(
        args.job_id,
        args.lines,
        pattern=args.pattern,
        context=args.context,
        start=args.start,
    )


def _command_job_artifacts(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).job_artifacts(args.job_id)


def _command_job_kill(args: argparse.Namespace) -> BaseModel:
    return _jobs(args).kill_job(args.job_id)


# -- campaigns ------------------------------------------------------------


def _command_list(args: argparse.Namespace) -> BaseModel:
    return CampaignService(
        store=JsonlCampaignStore(layout.campaigns_dir(_state_dir(args))),
        features=ParserFeatureReader(),
        now=system_now,
        repo_state=repo_state,
    ).list_campaigns()


def _command_run(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args), args).run(
        campaign_id=_campaign_id(args)
    )


def _command_pause(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args), args).pause(
        campaign_id=_campaign_id(args), kill=args.kill
    )


def _command_retry(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args), args).retry_units(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        reason=args.reason,
    )


def _command_create(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args), args).create_campaign(
        campaign_id=_campaign_id(args),
        repo_root=_repo_root(args),
        releases=args.release or (),
        machine_types=args.machine_type or (),
        features=args.feature or (),
        scenarios=args.scenario or (),
        install_from=args.install_from,
        max_jobs={
            pool: getattr(args, "max_{}_jobs".format(pool))
            for pool in Pool
            if getattr(args, "max_{}_jobs".format(pool)) is not None
        },
    )


def _command_status(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args)).campaign_status(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        group_by=args.group_by,
        problems_limit=args.problems_limit,
    )


def _command_units(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args)).list_units(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        limit=args.limit,
    )


def _command_history(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args)).unit_history(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        limit=args.limit,
    )


def _command_export(args: argparse.Namespace) -> BaseModel:
    state = _state_dir(args)
    campaign_id = _campaign_id(args)
    output = args.output or layout.export_file(state, campaign_id)
    return _service(_campaign_dir(args)).export_campaign(
        campaign_id=campaign_id,
        output=Path(output).expanduser().resolve(),
        jobs=JobDirFiles(layout.jobs_dir(state)),
        writer=TarBundleWriter(),
    )


def _job_service(state_dir: Path) -> JobService:
    """The job service over this checkout's state directory, the same as
    the server and every job process build."""
    workspace = LocalWorkspace(state_dir)
    return JobService(
        workspace=workspace,
        config=FileConfigStore(workspace.resolve_state_dir),
        feature_reader=LocalFeatureFileReader(),
        results=LocalJobResultStoreFactory(),
        lanes=FileLanePool(
            lambda: layout.lanes_dir(workspace.resolve_state_dir())
        ),
        launcher=JobProcessLauncher(workspace.resolve_jobs_dir),
        reconcile_lock=FileReconcileLock(workspace.resolve_state_dir),
        monotonic=time.monotonic,
        sleep=time.sleep,
        now_utc=system_now,
        new_job_id=lambda: uuid.uuid4().hex[:8],
    )


def _command_config(args: argparse.Namespace) -> BaseModel:
    service = _job_service(_state_dir(args))
    if args.action == "set":
        value = sys.stdin.read().strip() if args.value == "-" else args.value
        return service.set_config(args.key, value)
    if args.action == "unset":
        return service.set_config(args.key, None)
    return service.config_view()


def _reconcile(args: argparse.Namespace) -> None:
    """Bring every campaign up to date and start any queued job with a free
    lane, as every front-end does."""
    try:
        state = _state_dir(args)
    except ValueError:
        # No checkout named: there is no state directory to reconcile.
        return
    try:
        CampaignService(
            store=JsonlCampaignStore(layout.campaigns_dir(state)),
            features=ParserFeatureReader(),
            now=system_now,
            repo_state=repo_state,
            jobs=_job_runner(_job_service(state)),
        ).reconcile()
    except (JobServiceError, CampaignError, OSError) as error:
        # The command's own answer stands; the next caller retries.
        print("reconcile failed: {}".format(error), file=sys.stderr)


def _read_events(
    args: argparse.Namespace, since_seq: int
) -> AwaitEventsResponse:
    return _service(_campaign_dir(args)).await_events(
        campaign_id=_campaign_id(args),
        since_seq=since_seq,
        kinds=args.kinds or (),
        limit=args.limit,
    )


def _command_events(args: argparse.Namespace) -> Output:
    if not args.follow:
        return _read_events(args, args.since_seq)
    return _follow_events(args)


def _follow_events(args: argparse.Namespace) -> Iterator[BaseModel]:
    """Print each new batch until the campaign stops: paused or complete.

    Each poll reconciles first, so following a campaign from a terminal
    keeps it moving when no server runs.
    """
    since_seq = args.since_seq
    while True:
        _reconcile(args)
        batch = _read_events(args, since_seq)
        if batch.events:
            yield batch
            since_seq = batch.next_seq
            continue
        if batch.state in (CampaignState.PAUSED, CampaignState.COMPLETE):
            return
        time.sleep(args.interval)


UNIT_COLUMNS = (
    "feature",
    "scenario",
    "release",
    "machine_type",
    "state",
    "job_id",
    "attempt_count",
)
ATTEMPT_COLUMNS = (
    "attempt",
    "job_id",
    "install_from",
    "started_at",
    "outcome",
    "finished_at",
    "interrupted",
)


def _unit_rows(response: BaseModel) -> tuple[Sequence[str], list[dict]]:
    """One row per unit."""
    units = getattr(response, "units")
    return UNIT_COLUMNS, [unit.model_dump() for unit in units]


def _attempt_rows(response: BaseModel) -> tuple[Sequence[str], list[dict]]:
    """One row per attempt; a unit never attempted still gets one row."""
    columns = (*UNIT_COLUMNS[:5], *ATTEMPT_COLUMNS)
    rows = []
    for unit in getattr(response, "units"):
        base = {key: getattr(unit, key) for key in UNIT_COLUMNS[:5]}
        if not unit.attempts:
            rows.append(base)
        for number, attempt in enumerate(unit.attempts, 1):
            rows.append({**base, "attempt": number, **attempt.model_dump()})
    return columns, rows


def _write_csv(response: BaseModel, args: argparse.Namespace) -> None:
    columns, rows = args.csv_rows(response)
    writer = csv.DictWriter(sys.stdout, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    if getattr(response, "truncated", False):
        print(
            "listing cut at --limit {}; more units match".format(args.limit),
            file=sys.stderr,
        )


def _add_format(
    parser: argparse.ArgumentParser, rows: Callable[[BaseModel], Any]
) -> None:
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="json (default), or csv with one row per item",
    )
    parser.set_defaults(csv_rows=rows)


def _add_filters(
    parser: argparse.ArgumentParser, with_state: bool = True
) -> None:
    parser.add_argument("--feature", action="append")
    parser.add_argument("--scenario", action="append")
    parser.add_argument("--release", action="append")
    parser.add_argument("--machine-type", action="append", dest="machine_type")
    if with_state:
        parser.add_argument("--state", action="append", choices=STATES)


def _add_command(
    subparsers: Any,
    name: str,
    help_text: str,
    handler: Callable[[argparse.Namespace], BaseModel],
    with_state: bool = True,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    _add_campaign(parser)
    _add_filters(parser, with_state=with_state)
    parser.set_defaults(handler=handler)
    return parser


def _add_campaign(parser: argparse.ArgumentParser) -> None:
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--campaign-id",
        dest="campaign_id",
        metavar="ID",
        help=(
            "the campaign, by the name the server uses: "
            "<repo-root>/{}/{}/ID/".format(
                layout.DEFAULT_STATE_DIR_NAME, layout.CAMPAIGNS_SUBDIR
            )
        ),
    )
    which.add_argument(
        "--campaign",
        type=Path,
        dest="campaign_dir",
        metavar="DIR",
        help="a campaign directory anywhere; its id is the directory's name",
    )
    _add_repo_root(parser)


def _add_repo_root(
    parser: argparse.ArgumentParser, *, nested: bool = False
) -> None:
    """``--repo-root``. ``nested`` is for a subcommand of a parser that
    already takes it, so it is accepted in either place."""
    parser.add_argument(
        "--repo-root",
        type=Path,
        dest="repo_root",
        metavar="DIR",
        default=argparse.SUPPRESS if nested else None,
        help=(
            "ubuntu-pro-client checkout holding features/ and the server's "
            "state (default: ${})".format(layout.REPO_ENV_VAR)
        ),
    )


def _group(subparsers: Any, name: str, help_text: str) -> Any:
    """A noun with its own verbs, as ``job`` or ``campaign``."""
    parser = subparsers.add_parser(name, help=help_text)
    return parser.add_subparsers(dest="verb", required=True, metavar="VERB")


def _verb(
    verbs: Any,
    name: str,
    help_text: str,
    handler: Callable[[argparse.Namespace], Any],
) -> argparse.ArgumentParser:
    parser = verbs.add_parser(name, help=help_text)
    _add_repo_root(parser)
    parser.set_defaults(handler=handler)
    return parser


def _add_catalogue_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release")
    parser.add_argument("--machine-type", dest="machine_type")
    parser.add_argument("--tag")
    parser.add_argument("--text", help="a substring of the scenario name")


def _add_wait(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-wait",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        dest="max_wait",
        metavar="SECONDS",
        help="how long to wait before returning with timed_out",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_WAIT_POLL_INTERVAL_SECONDS,
        metavar="SECONDS",
    )


def _add_job_commands(subparsers: Any) -> None:
    jobs = _group(subparsers, "job", "run behave jobs and read what they did")

    run = _verb(jobs, "run", "queue one unit's job", _command_job_run)
    run.add_argument("feature", help="a path that feature list returns")
    run.add_argument("scenario", help="a scenario's exact name")
    run.add_argument("--release", required=True)
    run.add_argument("--machine-type", required=True, dest="machine_type")
    run.add_argument(
        "--install-from",
        default=DEFAULT_INSTALL_FROM,
        choices=INSTALL_FROM_VALUES,
        dest="install_from",
    )
    run.add_argument(
        "--wait", action="store_true", help="wait for the job to end"
    )
    _add_wait(run)

    listed = _verb(
        jobs,
        "list",
        "queued and running jobs, then ended ones",
        _command_job_list,
    )
    listed.add_argument("--limit", type=int, default=DEFAULT_JOB_LIST_LIMIT)

    results = _verb(
        jobs, "results", "what came of each job", _command_job_results
    )
    results.add_argument("--job-id", action="append", dest="job_id")
    results.add_argument("--feature")
    results.add_argument("--scenario", help="a substring of the name")
    results.add_argument("--release")
    results.add_argument("--machine-type", dest="machine_type")
    results.add_argument("--state", choices=[s.value for s in JobState])
    results.add_argument("--limit", type=int, default=DEFAULT_RESULTS_LIMIT)

    for name, help_text, handler in (
        ("wait", "wait for a job to end", _command_job_wait),
        (
            "errors",
            "what went wrong in a job, from its log",
            _command_job_errors,
        ),
        ("logs", "part of a job's log", _command_job_logs),
        ("artifacts", "a job's files and records", _command_job_artifacts),
        ("kill", "kill a queued or running job", _command_job_kill),
    ):
        verb = _verb(jobs, name, help_text, handler)
        verb.add_argument("job_id", metavar="JOB_ID")
        if name == "wait":
            _add_wait(verb)
        if name == "logs":
            verb.add_argument("--lines", type=int, default=DEFAULT_LOG_LINES)
            verb.add_argument("--pattern", help="a regex to search for")
            verb.add_argument(
                "--context", type=int, default=DEFAULT_LOG_CONTEXT
            )
            verb.add_argument("--start", type=int, default=0)


def _add_catalogue_commands(subparsers: Any) -> None:
    features = _group(subparsers, "feature", "the feature files")
    listed = _verb(
        features, "list", "every feature file", _command_feature_list
    )
    _add_catalogue_filters(listed)
    described = _verb(
        features,
        "describe",
        "one feature's scenarios",
        _command_feature_describe,
    )
    described.add_argument("feature", help="a path that feature list returns")

    scenarios = _group(subparsers, "scenario", "scenarios across features")
    found = _verb(
        scenarios, "find", "scenarios matching filters", _command_scenario_find
    )
    _add_catalogue_filters(found)
    found.add_argument("--limit", type=int, default=DEFAULT_FIND_LIMIT)

    dimensions = _group(
        subparsers, "dimension", "the releases and machine types in use"
    )
    _verb(
        dimensions,
        "list",
        "every release and machine type, with scenario counts",
        _command_dimension_list,
    )


def _add_campaign_commands(subparsers: Any) -> None:
    campaigns = _group(
        subparsers, "campaign", "plan, run and report on campaigns"
    )

    create = _add_command(
        campaigns,
        "create",
        "create the campaign from the feature files",
        _command_create,
        with_state=False,
    )
    create.add_argument(
        "--install-from",
        default=DEFAULT_INSTALL_FROM,
        choices=INSTALL_FROM_VALUES,
        dest="install_from",
        help=(
            "install source every job in this campaign should use "
            "(default: {})".format(DEFAULT_INSTALL_FROM)
        ),
    )
    for pool in Pool:
        create.add_argument(
            "--max-{}-jobs".format(pool),
            type=int,
            dest="max_{}_jobs".format(pool),
            metavar="N",
            help=(
                "how many of this campaign's {} jobs may be in flight at "
                "once (default: 1 if the scope covers the {} pool)".format(
                    pool, pool
                )
            ),
        )

    _verb(campaigns, "list", "every campaign on this checkout", _command_list)

    run = campaigns.add_parser("run", help="set the intent to run")
    _add_campaign(run)
    run.set_defaults(handler=_command_run)

    pause = campaigns.add_parser("pause", help="set the intent to hold")
    _add_campaign(pause)
    pause.add_argument(
        "--kill", action="store_true", help="kill the jobs in flight"
    )
    pause.set_defaults(handler=_command_pause)

    retry = _add_command(
        campaigns,
        "retry",
        "ask for another attempt at units, and run",
        _command_retry,
    )
    retry.add_argument("--reason", default="")

    status = _add_command(
        campaigns, "status", "show current state per unit", _command_status
    )
    status.add_argument(
        "--problems",
        type=int,
        default=MAX_UNITS_LIMIT,
        dest="problems_limit",
        metavar="N",
        help="list at most N problem rows (default: all)",
    )
    status.add_argument(
        "--group-by",
        default=GroupBy.UNIT.value,
        choices=GROUPINGS,
        dest="group_by",
        help=(
            "list problems one unit per row, or one scenario per row with "
            "its units by state (default: unit)"
        ),
    )

    units = _add_command(
        campaigns, "units", "list every unit with its state", _command_units
    )
    units.add_argument("--limit", type=int, default=DEFAULT_UNITS_LIMIT)
    _add_format(units, _unit_rows)

    history = _add_command(
        campaigns, "history", "show every attempt per unit", _command_history
    )
    history.add_argument("--limit", type=int, default=DEFAULT_UNITS_LIMIT)
    _add_format(history, _attempt_rows)

    exported = campaigns.add_parser(
        "export",
        help=(
            "write a complete campaign's evidence bundle: a summary for "
            "review, plus every job's files and the raw records"
        ),
    )
    _add_campaign(exported)
    exported.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help=(
            "where to write the .tar.gz; an existing file is never "
            "replaced (default: <state dir>/{}/ID.tar.gz)".format(
                layout.EXPORTS_SUBDIR
            )
        ),
    )
    exported.set_defaults(handler=_command_export)

    events = campaigns.add_parser(
        "events", help="read the events a campaign's records announce"
    )
    _add_campaign(events)
    events.add_argument(
        "--since-seq",
        type=int,
        default=0,
        dest="since_seq",
        help="return events numbered above this (default: all)",
    )
    events.add_argument(
        "--kinds",
        action="append",
        metavar="KIND",
        help=(
            "an event kind, family or preset to return, repeatable; omit "
            "for all. Families: {}. Presets: {}".format(
                ", ".join(EVENT_FAMILIES), ", ".join(EVENT_PRESETS)
            )
        ),
    )
    events.add_argument("--limit", type=int, default=DEFAULT_EVENTS_LIMIT)
    events.add_argument(
        "--follow",
        action="store_true",
        help=(
            "keep printing batches, one JSON document per line, until the "
            "campaign is paused or complete, reconciling as it goes"
        ),
    )
    events.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between polls with --follow (default: 5)",
    )
    events.set_defaults(handler=_command_events)


def _add_config_command(subparsers: Any) -> None:
    config = subparsers.add_parser(
        "config",
        help=(
            "show or change this checkout's config: max jobs per pool, and "
            "the behave options every job runs with"
        ),
    )
    _add_repo_root(config)
    actions = config.add_subparsers(dest="action")
    shower = actions.add_parser("show", help="show the config (the default)")
    _add_repo_root(shower, nested=True)
    setter = actions.add_parser(
        "set", help="set max_jobs.<pool> or behave.<option>"
    )
    _add_repo_root(setter, nested=True)
    setter.add_argument("key", metavar="KEY")
    setter.add_argument(
        "value",
        metavar="VALUE",
        help="the value, or - to read it from stdin (for secrets)",
    )
    unsetter = actions.add_parser(
        "unset",
        help="return max_jobs.<pool> to its default, or drop an option",
    )
    unsetter.add_argument("key", metavar="KEY")
    _add_repo_root(unsetter, nested=True)
    config.set_defaults(handler=_command_config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pro-behave",
        description=(
            "Run behave jobs and campaigns on an ubuntu-pro-client checkout."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="NOUN")
    _add_job_commands(subparsers)
    _add_campaign_commands(subparsers)
    _add_catalogue_commands(subparsers)
    _add_config_command(subparsers)
    subparsers.add_parser(
        "mcp",
        help=(
            "serve the MCP tools over streamable HTTP, until stopped; "
            "PRO_BEHAVE_MCP_HOST and PRO_BEHAVE_MCP_PORT set the address"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp":
        # Imported here: the MCP framework loads only for the server.
        from pro_behave.mcp import server

        server.main()
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_usage(sys.stderr)
        print("a command is required", file=sys.stderr)
        return 2

    try:
        result = handler(args)
        if getattr(args, "format", "json") == "csv":
            assert isinstance(result, BaseModel)
            _write_csv(result, args)
            return 0
        outputs = [result] if isinstance(result, BaseModel) else result
        for output in outputs:
            json.dump(output.model_dump(), sys.stdout, sort_keys=True)
            sys.stdout.write("\n")
            sys.stdout.flush()
    except (ValueError, OSError, JobServiceError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        _reconcile(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
