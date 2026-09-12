"""Command line interface over the campaign record."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .discovery import available_dimensions, discover_units
from .domain import (
    INSTALL_SOURCES,
    STATES,
    AttemptRecord,
    CampaignError,
    CampaignHeader,
    Filters,
    PlanRecord,
    Record,
    UnitStatus,
    attempts_from_mcp,
    count_states,
    describe_units,
    encode_record,
    parse_attempt,
    parse_record,
    problems,
    reduce_units,
    running,
    select_next,
)
from .repo import repo_state


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_records(campaign_file: Path) -> list[Record]:
    if not campaign_file.exists():
        return []

    records: list[Record] = []
    with campaign_file.open("r") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                records.append(parse_record(json.loads(line)))
            except ValueError as error:
                raise CampaignError(
                    "invalid campaign record on line {}: {}".format(
                        number, error
                    )
                ) from error
    return records


def _append_records(campaign_file: Path, records: Sequence[Record]) -> None:
    campaign_file.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(
            encode_record(record), sort_keys=True, separators=(",", ":")
        )
        + "\n"
        for record in records
    )
    with campaign_file.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


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


def _status_payload(
    statuses: Sequence[UnitStatus], campaign: CampaignHeader | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "counts": count_states(statuses),
        "running": [status.as_dict() for status in running(statuses)],
        "problems": [status.as_dict() for status in problems(statuses)],
    }
    if campaign is not None:
        payload["campaign"] = encode_record(campaign)
    return payload


def _campaign_of(records: Sequence[Record]) -> CampaignHeader | None:
    for record in records:
        if isinstance(record, CampaignHeader):
            return record
    return None


def _selected(
    statuses: Sequence[UnitStatus], filters: Filters
) -> list[UnitStatus]:
    return [status for status in statuses if filters.matches(status)]


def _command_dimensions(args: argparse.Namespace) -> dict[str, Any]:
    return available_dimensions(args.repo_root)


def _command_create(args: argparse.Namespace) -> dict[str, Any]:
    existing = _read_records(args.campaign_file)
    if existing:
        raise CampaignError(
            "this file already holds a campaign; create a new one instead"
        )

    filters = _filters(args)
    repo_root = args.repo_root.resolve()
    units = discover_units(repo_root, filters)
    if not units:
        raise CampaignError("no test units matched the requested filters")

    at = _now()
    campaign = CampaignHeader(
        at=at,
        campaign_id=args.campaign_id,
        repo=repo_state(repo_root),
        filters=filters,
    )
    records: list[Record] = [
        campaign,
        *(PlanRecord(unit=unit, at=at) for unit in units),
    ]
    _append_records(args.campaign_file, records)
    return _status_payload(reduce_units(records), campaign)


def _command_record(args: argparse.Namespace) -> dict[str, Any]:
    payload = _load_json(args.input)
    if args.from_mcp:
        parsed: list[AttemptRecord] = list(attempts_from_mcp(payload))
    else:
        if not isinstance(payload, list) or not payload:
            raise CampaignError("input must be a non-empty JSON array")
        parsed = [parse_attempt(raw) for raw in payload]

    at = _now()
    attempts: list[AttemptRecord] = [
        replace(attempt, install_from=args.install_from, at=at)
        for attempt in parsed
    ]
    existing = _read_records(args.campaign_file)
    planned = {r.unit for r in existing if isinstance(r, PlanRecord)}
    unplanned = {a.unit for a in attempts if a.unit not in planned}
    if unplanned:
        raise CampaignError(
            "{} attempt(s) reference unplanned units: {}".format(
                len(unplanned), describe_units(unplanned)
            )
        )

    _append_records(args.campaign_file, attempts)
    statuses = reduce_units([*existing, *attempts])
    return _status_payload(_selected(statuses, _filters(args)))


def _command_status(args: argparse.Namespace) -> dict[str, Any]:
    records = _read_records(args.campaign_file)
    statuses = reduce_units(records)
    return _status_payload(
        _selected(statuses, _filters(args)), _campaign_of(records)
    )


def _command_next(args: argparse.Namespace) -> dict[str, Any]:
    statuses = reduce_units(_read_records(args.campaign_file))
    selected = select_next(_selected(statuses, _filters(args)), args.limit)
    return {"units": [status.as_dict() for status in selected]}


def _command_history(args: argparse.Namespace) -> dict[str, Any]:
    statuses = reduce_units(_read_records(args.campaign_file))
    return {
        "units": [
            status.as_dict(include_attempts=True)
            for status in _selected(statuses, _filters(args))
        ]
    }


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
    handler: Callable[[argparse.Namespace], dict[str, Any]],
    with_state: bool = True,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument(
        "--campaign", required=True, type=Path, dest="campaign_file"
    )
    _add_filters(parser, with_state=with_state)
    parser.set_defaults(handler=handler)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track behave test units and attempts."
    )
    subparsers = parser.add_subparsers(dest="command")

    dimensions = subparsers.add_parser(
        "dimensions", help="list the releases and machine types available"
    )
    dimensions.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        dest="repo_root",
        help="ubuntu-pro-client checkout holding features/",
    )
    dimensions.set_defaults(handler=_command_dimensions)

    create = _add_command(
        subparsers,
        "create",
        "create the campaign from the feature files",
        _command_create,
        with_state=False,
    )
    create.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        dest="repo_root",
        help="ubuntu-pro-client checkout holding features/",
    )
    create.add_argument(
        "--campaign-id",
        default=None,
        dest="campaign_id",
        help="identifier for this campaign, such as an SRU bug number",
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

    _add_command(
        subparsers, "status", "show current state per unit", _command_status
    )

    following = _add_command(
        subparsers, "next", "show the units to run next", _command_next
    )
    following.add_argument("--limit", type=int, default=1)

    _add_command(
        subparsers, "history", "show every attempt per unit", _command_history
    )
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

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
