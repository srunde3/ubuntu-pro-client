"""Command line interface over the campaign record.

Every command is a handler: read arguments, call ``CampaignService``, print
the response as JSON. Behaviour lives in the service, so the CLI and the MCP
tools cannot drift apart. The only work done here is the work that is
genuinely a command line's: parsing arguments and reading stdin.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from pydantic import BaseModel

from behave_campaign.domain.events import EVENT_FAMILIES, EVENT_PRESETS
from behave_campaign.domain.records import (
    DEFAULT_INSTALL_FROM,
    INSTALL_FROM_VALUES,
    STATES,
    CampaignState,
    Filters,
)
from behave_campaign.domain.state import GROUPINGS, GroupBy
from behave_mcp.domain import layout
from behave_mcp.domain.pools import Pool

from .adapters import (
    CampaignDirStore,
    JobDirFiles,
    ParserFeatureReader,
    TarBundleWriter,
    system_now,
)
from .messages import (
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_UNITS_LIMIT,
    MAX_UNITS_LIMIT,
    AwaitEventsResponse,
)
from .repo import repo_state
from .service import CampaignService

Output = BaseModel | Iterator[BaseModel]


def _service(campaign_dir: Path) -> CampaignService:
    return CampaignService(
        store=CampaignDirStore(campaign_dir),
        features=ParserFeatureReader(),
        now=system_now,
        repo_state=repo_state,
        # Nothing here runs tests, so no lane ceiling applies.
        lane_ceiling=None,
    )


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


def _command_dimensions(args: argparse.Namespace) -> BaseModel:
    # dimensions reads feature files, not a campaign, so this command takes
    # no --campaign and the store it is given is never touched.
    return _service(Path()).dimensions(repo_root=_repo_root(args))


def _command_create(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_dir(args)).create_campaign(
        campaign_id=_campaign_id(args),
        repo_root=_repo_root(args),
        releases=args.release or (),
        machine_types=args.machine_type or (),
        feature_files=args.feature or (),
        scenarios=args.scenario or (),
        install_from=args.install_from,
        lanes={
            pool: getattr(args, "{}_lanes".format(pool))
            for pool in Pool
            if getattr(args, "{}_lanes".format(pool)) is not None
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
    """Print each new batch until the campaign stops: paused or complete."""
    since_seq = args.since_seq
    while True:
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


def _add_repo_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        type=Path,
        dest="repo_root",
        metavar="DIR",
        help=(
            "ubuntu-pro-client checkout holding features/ and the server's "
            "state (default: ${})".format(layout.REPO_ENV_VAR)
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track behave test units and attempts."
    )
    subparsers = parser.add_subparsers(dest="command")

    dimensions = subparsers.add_parser(
        "dimensions", help="list the releases and machine types available"
    )
    _add_repo_root(dimensions)
    dimensions.set_defaults(handler=_command_dimensions)

    create = _add_command(
        subparsers,
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
            "--{}-lanes".format(pool),
            type=int,
            dest="{}_lanes".format(pool),
            metavar="N",
            help=(
                "how many {} jobs a runner may keep in flight for this "
                "campaign (default: 1 if the scope covers the {} pool)".format(
                    pool, pool
                )
            ),
        )

    status = _add_command(
        subparsers, "status", "show current state per unit", _command_status
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
        subparsers, "units", "list every unit with its state", _command_units
    )
    units.add_argument("--limit", type=int, default=DEFAULT_UNITS_LIMIT)
    _add_format(units, _unit_rows)

    history = _add_command(
        subparsers, "history", "show every attempt per unit", _command_history
    )
    history.add_argument("--limit", type=int, default=DEFAULT_UNITS_LIMIT)
    _add_format(history, _attempt_rows)

    exported = subparsers.add_parser(
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

    events = subparsers.add_parser(
        "events", help="read the events a running server has announced"
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
            "campaign is paused or complete"
        ),
    )
    events.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between polls with --follow (default: 5)",
    )
    events.set_defaults(handler=_command_events)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
