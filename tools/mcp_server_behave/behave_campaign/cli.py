"""Command line interface over the campaign record.

Every command is a wrapper: read arguments, call ``CampaignService``, print
the response as JSON. Behaviour lives in the service, so the CLI and the MCP
tools cannot drift apart. The only work done here is the work that is
genuinely a command line's: parsing arguments and reading stdin.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from pydantic import BaseModel

from behave_mcp import layout

from .adapters import (
    JsonlEventLog,
    NullEventLog,
    ParserFeatureReader,
    SingleFileCampaignStore,
    system_now,
)
from .domain import (
    DEFAULT_INSTALL_SOURCE,
    EVENT_FAMILIES,
    EVENT_PRESETS,
    GROUPINGS,
    INSTALL_SOURCES,
    STATES,
    Filters,
    GroupBy,
    Lifecycle,
)
from .messages import (
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_UNITS_LIMIT,
    MAX_UNITS_LIMIT,
    AwaitEventsResponse,
)
from .ports import EventLog
from .repo import repo_state
from .service import CampaignService

Output = BaseModel | Iterator[BaseModel]


def _service(
    campaign_file: Path, events: EventLog | None = None
) -> CampaignService:
    return CampaignService(
        store=SingleFileCampaignStore(campaign_file),
        features=ParserFeatureReader(),
        # The CLI reports to someone already reading its output, so it
        # writes to no notification channel; only ``events`` reads one.
        events=events or NullEventLog(),
        now=system_now,
        repo_state=repo_state,
        # Nothing here runs tests, so no lane ceiling applies.
        max_lane_ceiling=None,
    )


def _event_log(campaign_file: Path) -> JsonlEventLog:
    """The log a server writes beside the campaign file.

    Built fresh per read: the log caches its file, and the process
    appending to it is not this one.
    """
    return JsonlEventLog(campaign_file.parent)


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


def _campaign_file(args: argparse.Namespace) -> Path:
    """The campaign's file: given outright, or the server's file for an id.

    The id names the file the way the server names it, so a human and the
    server read the same record by the same name.
    """
    if args.campaign_file is not None:
        return args.campaign_file
    return layout.campaign_file(_repo_root(args), args.campaign_id)


def _campaign_id(args: argparse.Namespace) -> str:
    """The campaign's id: always its file's stem."""
    return _campaign_file(args).stem


def _load_json(source: str) -> Any:
    if source == "-":
        return json.load(sys.stdin)
    with open(source, "r") as stream:
        return json.load(stream)


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
    return _service(_campaign_file(args)).create_campaign(
        campaign_id=_campaign_id(args),
        repo_root=_repo_root(args),
        releases=args.release or (),
        machine_types=args.machine_type or (),
        feature_files=args.feature or (),
        scenarios=args.scenario or (),
        install_from=args.install_from,
        max_lanes=args.max_lanes,
    )


def _command_record(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_file(args)).record_attempts(
        campaign_id=_campaign_id(args),
        payload=_load_json(args.input),
        install_from=args.install_from,
        from_mcp=args.from_mcp,
    )


def _command_status(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_file(args)).campaign_status(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        units_limit=args.units_limit,
        group_by=args.group_by,
        problems_limit=args.problems_limit,
    )


def _command_next(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_file(args)).next_units(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        limit=args.limit,
    )


def _command_history(args: argparse.Namespace) -> BaseModel:
    return _service(_campaign_file(args)).unit_history(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        limit=args.limit,
    )


def _read_events(
    args: argparse.Namespace, since_seq: int
) -> AwaitEventsResponse:
    return _service(
        args.campaign_file, events=_event_log(_campaign_file(args))
    ).await_events(
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
    """Print each new batch until the campaign has nothing more to say."""
    since_seq = args.since_seq
    while True:
        batch = _read_events(args, since_seq)
        if batch.events:
            yield batch
            since_seq = batch.next_seq
            continue
        settled = batch.lifecycle in (
            Lifecycle.COMPLETE,
            Lifecycle.CANCELLED,
        )
        if settled and batch.lanes_busy == 0:
            return
        time.sleep(args.interval)


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
            "<repo-root>/{}/{}/ID{}".format(
                layout.DEFAULT_STATE_DIR_NAME,
                layout.CAMPAIGNS_SUBDIR,
                layout.CAMPAIGN_SUFFIX,
            )
        ),
    )
    which.add_argument(
        "--campaign",
        type=Path,
        dest="campaign_file",
        metavar="FILE",
        help="a campaign file anywhere; its id is the file's name",
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
        default=DEFAULT_INSTALL_SOURCE,
        choices=INSTALL_SOURCES,
        dest="install_from",
        help=(
            "install source every job in this campaign should use "
            "(default: {})".format(DEFAULT_INSTALL_SOURCE)
        ),
    )
    create.add_argument(
        "--max-lanes",
        default=1,
        type=int,
        dest="max_lanes",
        help=(
            "how many jobs a runner may keep in flight for this campaign "
            "(default: 1)"
        ),
    )

    record = _add_command(
        subparsers, "record", "append attempt results", _command_record
    )
    record.add_argument(
        "--input", default="-", help="JSON file, or - for stdin (default)"
    )
    record.add_argument(
        "--from-mcp",
        action="store_true",
        dest="from_mcp",
        help="read [{unit, result}] MCP payloads instead of plain attempts",
    )
    record.add_argument(
        "--install-from",
        required=True,
        choices=INSTALL_SOURCES,
        dest="install_from",
        help="where the job installed ubuntu-pro-client from",
    )

    status = _add_command(
        subparsers, "status", "show current state per unit", _command_status
    )
    status.add_argument(
        "--units",
        type=int,
        default=0,
        dest="units_limit",
        metavar="N",
        help=(
            "list up to N individual units as well as the counts. "
            "Omit to report counts, in-flight units and problems only."
        ),
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

    following = _add_command(
        subparsers, "next", "show the units to run next", _command_next
    )
    following.add_argument("--limit", type=int, default=1)

    history = _add_command(
        subparsers, "history", "show every attempt per unit", _command_history
    )
    history.add_argument("--limit", type=int, default=DEFAULT_UNITS_LIMIT)

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
            "campaign is complete or cancelled with no lane in flight"
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
