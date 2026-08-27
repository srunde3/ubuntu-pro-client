#!/usr/bin/env python3
"""Parse Pro client ``behave`` feature files into typed, structured data.

This module provides structures for testing conventions used in the Pro client
that cannot be expressed by native ``behave``. For example, Pro client tests
are frequently executed on permutations of ``<release>`` and
``<machine_type>``.
This module provides a structured way to parse those facts from feature files.

As testing conventions change, this file and its tests should be kept current.
This module should be considered downstream of the feature tests; if feature
tests need to change their testing conventions, it is acceptable for this
module to change.
"""

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from behave.parser import parse_file

ALLOWED_MACHINE_TYPES = {
    "lxd-container",
    "lxd-vm",
    "aws.generic",
    "gcp.generic",
    "azure.generic",
    "aws.pro",
    "gcp.pro",
    "azure.pro",
    "aws.pro-fips",
    "gcp.pro-fips",
    "azure.pro-fips",
    "wsl",
}


@dataclass
class Combo:
    """A single valid ``(release, machine_type)`` pair for a scenario."""

    release: str
    machine_type: str


@dataclass
class ExamplesBlock:
    """One ``Examples:`` table within a Scenario Outline, with its own
    tags."""

    name: str
    tags: List[str]
    combos: List[Combo]


@dataclass
class ScenarioSummary:
    """A scenario's browse/select metadata (no step text).

    ``examples`` is the per-``Examples:``-block view; the other fields are
    unioned across all blocks.
    """

    name: str
    type: str
    tags: List[str]
    requires_config: List[str]
    example_columns: List[str]
    combos: List[Combo]
    examples: List[ExamplesBlock] = field(default_factory=list)


@dataclass
class FeatureDetail:
    """Full parsed metadata for one feature file."""

    path: str
    title: str
    tags: List[str]
    requires_config: List[str]
    scenarios: List[ScenarioSummary]


@dataclass
class FeatureCatalogEntry:
    """Lightweight catalog projection of a feature."""

    path: str
    title: str
    scenario_count: int
    requires_config: List[str]
    releases: List[str]
    machine_types: List[str]


@dataclass
class DimensionValue:
    """A distinct release or machine_type with its scenario count."""

    name: str
    scenario_count: int


@dataclass
class Dimensions:
    """Distinct releases and machine_types across the suite."""

    releases: List[DimensionValue]
    machine_types: List[DimensionValue]


@dataclass
class ScenarioMatch:
    """A scenario hit with the combos satisfying some filter."""

    feature_file: str
    scenario_name: str
    type: str
    requires_config: List[str]
    combos: List[Combo]


_CONFIG_TAG_PREFIX = "uses.config."
_GIVEN_MACHINE_STEP_RE = re.compile(
    r"a `(.*)` `(.*)` machine with ubuntu-advantage-tools installed"
)


def normalize_feature_file_arg(feature_file: str) -> str:
    normalized = posixpath.normpath(feature_file.replace("\\", "/"))
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def requires_config_from_tags(tags: List[str]) -> List[str]:
    """Return sorted config requirements from ``uses.config.*`` tags."""
    requirements: List[str] = []
    for tag in tags:
        if tag.startswith(_CONFIG_TAG_PREFIX):
            remainder = tag[len(_CONFIG_TAG_PREFIX) :]
            if remainder and remainder not in requirements:
                requirements.append(remainder)
    return sorted(requirements)


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _literal_or_none(value: str) -> Optional[str]:
    if not value or _is_placeholder(value):
        return None
    return value


def _step_machine_override(
    scenario: Any,
) -> Tuple[Optional[str], Optional[str]]:
    steps = getattr(scenario, "steps", None) or []
    if not steps:
        return None, None
    match = _GIVEN_MACHINE_STEP_RE.match(getattr(steps[0], "name", "") or "")
    if not match:
        return None, None
    return _literal_or_none(match.group(1)), _literal_or_none(match.group(2))


def _add_combo(
    combos: List[Combo],
    seen: set,
    release: Optional[str],
    machine_type: Optional[str],
) -> None:
    if not release or not machine_type:
        return
    if _is_placeholder(release) or _is_placeholder(machine_type):
        return
    key = (release, machine_type)
    if key in seen:
        return
    seen.add(key)
    combos.append(Combo(release=release, machine_type=machine_type))


def _combos_from_table(
    table: Any,
    step_release: Optional[str],
    step_machine_type: Optional[str],
) -> List[Combo]:
    combos: List[Combo] = []
    seen: set = set()
    headings = list(getattr(table, "headings", []) or [])
    for row in getattr(table, "rows", []) or []:
        row_map = dict(zip(headings, list(getattr(row, "cells", []))))
        _add_combo(
            combos,
            seen,
            step_release or row_map.get("release"),
            step_machine_type or row_map.get("machine_type"),
        )
    return combos


def combos_from_scenario(scenario: Any) -> List[Combo]:
    """Return distinct combos for a scenario, unioned across examples."""
    step_release, step_machine_type = _step_machine_override(scenario)
    combos: List[Combo] = []
    seen: set = set()

    examples = getattr(scenario, "examples", None) or []
    if examples:
        for example in examples:
            table = getattr(example, "table", None)
            if table is None:
                continue
            for combo in _combos_from_table(
                table, step_release, step_machine_type
            ):
                _add_combo(combos, seen, combo.release, combo.machine_type)
    else:
        _add_combo(combos, seen, step_release, step_machine_type)

    return combos


def examples_blocks_from_scenario(scenario: Any) -> List[ExamplesBlock]:
    """Return one ``ExamplesBlock`` per ``Examples:`` table."""
    step_release, step_machine_type = _step_machine_override(scenario)
    blocks: List[ExamplesBlock] = []
    for example in getattr(scenario, "examples", None) or []:
        table = getattr(example, "table", None)
        combos = (
            _combos_from_table(table, step_release, step_machine_type)
            if table is not None
            else []
        )
        blocks.append(
            ExamplesBlock(
                name=getattr(example, "name", "") or "",
                tags=[
                    str(tag) for tag in (getattr(example, "tags", []) or [])
                ],
                combos=combos,
            )
        )
    return blocks


def _example_columns(scenario: Any) -> List[str]:
    columns: List[str] = []
    for example in getattr(scenario, "examples", None) or []:
        table = getattr(example, "table", None)
        if table is None:
            continue
        for heading in getattr(table, "headings", []) or []:
            if heading not in columns:
                columns.append(heading)
    return columns


def summarize_feature(feature: Any) -> FeatureDetail:
    """Turn a parsed behave ``Feature`` into a ``FeatureDetail``."""
    feature_tags = [str(tag) for tag in (getattr(feature, "tags", []) or [])]
    scenarios: List[ScenarioSummary] = []
    for scenario in getattr(feature, "scenarios", []) or []:
        scenario_tags = [
            str(tag) for tag in (getattr(scenario, "tags", []) or [])
        ]
        effective_tags = feature_tags + scenario_tags
        scenarios.append(
            ScenarioSummary(
                name=getattr(scenario, "name", "") or "",
                type=getattr(scenario, "type", "scenario"),
                tags=scenario_tags,
                requires_config=requires_config_from_tags(effective_tags),
                example_columns=_example_columns(scenario),
                combos=combos_from_scenario(scenario),
                examples=examples_blocks_from_scenario(scenario),
            )
        )
    return FeatureDetail(
        path="",
        title=getattr(feature, "name", "") or "",
        tags=feature_tags,
        requires_config=requires_config_from_tags(feature_tags),
        scenarios=scenarios,
    )


def catalog_entry(feature_detail: FeatureDetail) -> FeatureCatalogEntry:
    """Project a full feature detail into a lightweight catalog entry."""
    releases: List[str] = []
    machine_types: List[str] = []
    requires_config = list(feature_detail.requires_config)
    for scenario in feature_detail.scenarios:
        for requirement in scenario.requires_config:
            if requirement not in requires_config:
                requires_config.append(requirement)
        for combo in scenario.combos:
            if combo.release not in releases:
                releases.append(combo.release)
            if combo.machine_type not in machine_types:
                machine_types.append(combo.machine_type)
    return FeatureCatalogEntry(
        path=feature_detail.path,
        title=feature_detail.title,
        scenario_count=len(feature_detail.scenarios),
        requires_config=sorted(requires_config),
        releases=sorted(releases),
        machine_types=sorted(machine_types),
    )


def filtered_combos(
    scenario: ScenarioSummary,
    release: Optional[str] = None,
    machine_type: Optional[str] = None,
) -> List[Combo]:
    """Return the scenario combos matching the given release/machine_type."""
    result: List[Combo] = []
    for combo in scenario.combos:
        if release is not None and combo.release != release:
            continue
        if machine_type is not None and combo.machine_type != machine_type:
            continue
        result.append(combo)
    return result


def scenario_matches(
    scenario: ScenarioSummary,
    feature_tags: List[str],
    *,
    release: Optional[str] = None,
    machine_type: Optional[str] = None,
    tag: Optional[str] = None,
    text: Optional[str] = None,
) -> bool:
    """Return whether a scenario satisfies all provided filters."""
    if (release is not None or machine_type is not None) and not (
        filtered_combos(scenario, release, machine_type)
    ):
        return False
    if tag is not None:
        all_tags = set(feature_tags) | set(scenario.tags)
        if tag not in all_tags:
            return False
    if text is not None:
        if text.lower() not in scenario.name.lower():
            return False
    return True


def aggregate_dimensions(feature_details: List[FeatureDetail]) -> Dimensions:
    """Return distinct releases and machine_types with scenario counts."""
    release_counts: Dict[str, int] = {}
    machine_type_counts: Dict[str, int] = {}
    for feature_detail in feature_details:
        for scenario in feature_detail.scenarios:
            releases_seen: set = set()
            machine_types_seen: set = set()
            for combo in scenario.combos:
                releases_seen.add(combo.release)
                machine_types_seen.add(combo.machine_type)
            for release in releases_seen:
                release_counts[release] = release_counts.get(release, 0) + 1
            for machine_type in machine_types_seen:
                machine_type_counts[machine_type] = (
                    machine_type_counts.get(machine_type, 0) + 1
                )
    return Dimensions(
        releases=[
            DimensionValue(name=name, scenario_count=count)
            for name, count in sorted(release_counts.items())
        ],
        machine_types=[
            DimensionValue(name=name, scenario_count=count)
            for name, count in sorted(machine_type_counts.items())
        ],
    )


_detail_cache: Dict[str, Tuple[float, FeatureDetail]] = {}


def discover_feature_files(repo_root: Path) -> List[str]:
    """Return repo-relative paths of every ``*.feature`` file, sorted."""
    features_dir = repo_root / "features"
    if not features_dir.exists():
        return []

    return sorted(
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in features_dir.rglob("*.feature")
    )


def discover_feature_details(repo_root: Path) -> List[FeatureDetail]:
    """Return parsed metadata for every feature file, sorted by path."""
    features_dir = repo_root / "features"
    if not features_dir.exists():
        return []

    details: List[FeatureDetail] = []
    for path in sorted(features_dir.rglob("*.feature")):
        summary = _read_feature_detail(path)
        if summary is None:
            continue
        rel_path = str(path.relative_to(repo_root)).replace("\\", "/")
        details.append(_with_path(summary, rel_path))
    return sorted(details, key=lambda detail: detail.path)


def _with_path(detail: FeatureDetail, path: str) -> FeatureDetail:
    return FeatureDetail(
        path=path,
        title=detail.title,
        tags=detail.tags,
        requires_config=detail.requires_config,
        scenarios=detail.scenarios,
    )


def _read_feature_detail(path: Path) -> Optional[FeatureDetail]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None

    cache_key = str(path)
    cached = _detail_cache.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        feature = parse_file(str(path))
    except Exception:
        return None
    if feature is None:
        return None

    summary = summarize_feature(feature)
    _detail_cache[cache_key] = (mtime, summary)
    return summary
