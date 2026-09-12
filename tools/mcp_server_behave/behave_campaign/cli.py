"""Command line interface over the campaign record.

Every command is a wrapper: read arguments, call ``CampaignService``, print
the response as JSON. Behaviour lives in the service, so the CLI and the MCP
tools cannot drift apart. The only work done here is the work that is
genuinely a command line's: parsing arguments and reading stdin.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import BaseModel

from .adapters import (
    NullEventLog,
    ParserFeatureReader,
    SingleFileCampaignStore,
    system_now,
)
from .domain import DEFAULT_INSTALL_SOURCE, INSTALL_SOURCES, STATES, Filters
from .messages import DEFAULT_UNITS_LIMIT
from .repo import repo_state
from .service import CampaignService


def _service(campaign_file: Path) -> CampaignService:
    return CampaignService(
        store=SingleFileCampaignStore(campaign_file),
        features=ParserFeatureReader(),
        # The CLI reports to someone already reading its output, so it has
        # no use for a notification channel.
        events=NullEventLog(),
        now=system_now,
        repo_state=repo_state,
        # Nothing here runs tests, so no lane ceiling applies.
        max_lane_ceiling=None,
    )


def _campaign_id(args: argparse.Namespace) -> str:
    """The campaign's id, which the CLI takes from its file name."""
    return getattr(args, "campaign_id", None) or args.campaign_file.stem


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
    return _service(Path()).dimensions(repo_root=args.repo_root)


def _command_create(args: argparse.Namespace) -> BaseModel:
    return _service(args.campaign_file).create_campaign(
        campaign_id=_campaign_id(args),
        repo_root=args.repo_root.resolve(),
        releases=args.release or (),
        machine_types=args.machine_type or (),
        feature_files=args.feature or (),
        scenarios=args.scenario or (),
        install_from=args.install_from,
        max_lanes=args.max_lanes,
    )


def _command_record(args: argparse.Namespace) -> BaseModel:
    return _service(args.campaign_file).record_attempts(
        campaign_id=_campaign_id(args),
        payload=_load_json(args.input),
        install_from=args.install_from,
        from_mcp=args.from_mcp,
    )


def _command_status(args: argparse.Namespace) -> BaseModel:
    return _service(args.campaign_file).campaign_status(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        units_limit=args.units_limit,
    )


def _command_next(args: argparse.Namespace) -> BaseModel:
    return _service(args.campaign_file).next_units(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        limit=args.limit,
    )


def _command_history(args: argparse.Namespace) -> BaseModel:
    return _service(args.campaign_file).unit_history(
        campaign_id=_campaign_id(args),
        filters=_filters(args),
        limit=args.limit,
    )


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
    parser.add_argument(
        "--campaign", required=True, type=Path, dest="campaign_file"
    )
    _add_filters(parser, with_state=with_state)
    parser.set_defaults(handler=handler)
    return parser


def _add_repo_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        dest="repo_root",
        help="ubuntu-pro-client checkout holding features/",
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
    _add_repo_root(create)
    create.add_argument(
        "--campaign-id",
        default=None,
        dest="campaign_id",
        help=(
            "identifier for this campaign, such as an SRU bug number. "
            "Defaults to the campaign file's name."
        ),
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

    following = _add_command(
        subparsers, "next", "show the units to run next", _command_next
    )
    following.add_argument("--limit", type=int, default=1)

    history = _add_command(
        subparsers, "history", "show every attempt per unit", _command_history
    )
    history.add_argument("--limit", type=int, default=DEFAULT_UNITS_LIMIT)
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
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2

    json.dump(result.model_dump(), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
