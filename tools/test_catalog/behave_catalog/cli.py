#!/usr/bin/env python3
"""Command-line interface for the standalone behave test catalog."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from behave_catalog.db import QueryResult, TestCatalog

DEFAULT_DB_NAME = ".behave_test_catalog.sqlite3"


def _default_db_path(repo_root: Path) -> Path:
    return repo_root / DEFAULT_DB_NAME


def _detect_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "features").is_dir():
            return candidate.resolve()
    raise SystemExit(
        f"Could not find a 'features' directory above {start}; "
        "pass --repo-root explicitly."
    )


def _validate_repo_root(path: Path) -> Path:
    if not (path / "features").is_dir():
        raise SystemExit(
            f"--repo-root {path} does not look like an ubuntu-pro-client "
            "checkout (no 'features' directory found there)."
        )
    return path


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    repo_root = (
        _validate_repo_root(Path(args.repo_root).resolve())
        if args.repo_root
        else _detect_repo_root(Path.cwd())
    )
    db_path = (
        Path(args.db).resolve() if args.db else _default_db_path(repo_root)
    )
    return repo_root, db_path


def _print_records(records: Sequence[Any], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps([asdict(record) for record in records], indent=2))
        return
    if not records:
        print("(no matches)")
        return
    for record in records:
        print(record)


def _print_query_result(result: QueryResult, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(asdict(result), indent=2))
        return
    if not result.matches:
        print("(no matches)")
    for match in result.matches:
        print(match)
    if result.truncated:
        print(
            f"... truncated: showing {len(result.matches)} of "
            f"{result.total_count} total matches"
        )


def _cmd_build(args: argparse.Namespace) -> int:
    repo_root, db_path = _resolve_paths(args)
    catalog = TestCatalog.build(repo_root, db_path)
    catalog.close()
    print(f"Built catalog at {db_path}")
    return 0


def _cmd_features(args: argparse.Namespace) -> int:
    repo_root, db_path = _resolve_paths(args)
    with TestCatalog.open_or_build(repo_root, db_path) as catalog:
        entries = catalog.features(
            release=args.release,
            machine_type=args.machine_type,
            tag=args.tag,
            text=args.text,
        )
    _print_records(entries, args.format)
    return 0


def _cmd_dimensions(args: argparse.Namespace) -> int:
    repo_root, db_path = _resolve_paths(args)
    with TestCatalog.open_or_build(repo_root, db_path) as catalog:
        dims = catalog.dimensions()
    if args.format == "json":
        print(json.dumps(asdict(dims), indent=2))
        return 0
    print("releases:")
    for value in dims.releases:
        print(f"  {value.name} ({value.scenario_count})")
    print("machine_types:")
    for value in dims.machine_types:
        print(f"  {value.name} ({value.scenario_count})")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    repo_root, db_path = _resolve_paths(args)
    with TestCatalog.open_or_build(repo_root, db_path) as catalog:
        result = catalog.query(
            releases=args.release,
            machine_types=args.machine_type,
            tags_any=args.tag,
            text=args.text,
            limit=args.limit,
        )
    _print_query_result(result, args.format)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    repo_root, db_path = _resolve_paths(args)
    with TestCatalog.open_or_build(repo_root, db_path) as catalog:
        result = catalog.search(args.query, limit=args.limit)
    _print_query_result(result, args.format)
    return 0


def _cmd_describe(args: argparse.Namespace) -> int:
    repo_root, db_path = _resolve_paths(args)
    with TestCatalog.open_or_build(repo_root, db_path) as catalog:
        detail = catalog.feature_detail(args.feature_path)
    if detail is None:
        raise SystemExit(f"Feature not found in catalog: {args.feature_path}")
    if args.format == "json":
        print(json.dumps(asdict(detail), indent=2))
        return 0
    print(detail)
    return 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        default=None,
        help="ubuntu-pro-client checkout root (default: auto-detected).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Catalog database path "
            f"(default: <repo-root>/{DEFAULT_DB_NAME})."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="test-catalog")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_cmd = subparsers.add_parser(
        "build", help="(Re)build the catalog database from feature files."
    )
    _add_common_args(build_cmd)
    build_cmd.set_defaults(func=_cmd_build)

    features_cmd = subparsers.add_parser(
        "features", help="List features, optionally filtered."
    )
    _add_common_args(features_cmd)
    features_cmd.add_argument("--release")
    features_cmd.add_argument("--machine-type")
    features_cmd.add_argument("--tag")
    features_cmd.add_argument("--text")
    features_cmd.add_argument(
        "--format", choices=["table", "json"], default="table"
    )
    features_cmd.set_defaults(func=_cmd_features)

    describe_cmd = subparsers.add_parser(
        "describe", help="Full detail for one feature (every scenario)."
    )
    _add_common_args(describe_cmd)
    describe_cmd.add_argument(
        "feature_path", help="A path as returned by 'features'."
    )
    describe_cmd.add_argument(
        "--format", choices=["table", "json"], default="table"
    )
    describe_cmd.set_defaults(func=_cmd_describe)

    dimensions_cmd = subparsers.add_parser(
        "dimensions", help="List distinct releases and machine_types."
    )
    _add_common_args(dimensions_cmd)
    dimensions_cmd.add_argument(
        "--format", choices=["table", "json"], default="table"
    )
    dimensions_cmd.set_defaults(func=_cmd_dimensions)

    query_cmd = subparsers.add_parser(
        "query", help="Composable scenario query (AND of all given filters)."
    )
    _add_common_args(query_cmd)
    query_cmd.add_argument("--release", action="append", default=None)
    query_cmd.add_argument("--machine-type", action="append", default=None)
    query_cmd.add_argument("--tag", action="append", default=None)
    query_cmd.add_argument("--text")
    query_cmd.add_argument("--limit", type=int, default=200)
    query_cmd.add_argument(
        "--format", choices=["table", "json"], default="table"
    )
    query_cmd.set_defaults(func=_cmd_query)

    search_cmd = subparsers.add_parser(
        "search", help="Full-text keyword search over scenario content."
    )
    _add_common_args(search_cmd)
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=50)
    search_cmd.add_argument(
        "--format", choices=["table", "json"], default="table"
    )
    search_cmd.set_defaults(func=_cmd_search)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        # Covers narrow filesystem races (e.g. a .feature file deleted or
        # replaced between discovery and fingerprinting) and other
        # environment hiccups (permissions, disk full) -- an agent-facing
        # CLI should fail with a clean message, not a raw traceback.
        raise SystemExit(f"test-catalog: {exc}") from exc


if __name__ == "__main__":
    sys.exit(main())
