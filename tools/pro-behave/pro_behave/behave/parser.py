#!/usr/bin/env python3
"""Parse Pro client ``behave`` feature files into typed, structured data.

This module provides structures for testing conventions used in the Pro client
that cannot be expressed by native ``behave``. For example, Pro client tests
are frequently executed on combos of ``<release>`` and ``<machine_type>``.
This module provides a structured way to parse those facts from feature files.

It imports only ``behave`` and the standard library, so it can be lifted out
as a library; the behave adapter maps its types onto the tool's catalog.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from behave.parser import parse_file


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


_CONFIG_TAG_PREFIX = "uses.config."
_GIVEN_MACHINE_STEP_RE = re.compile(
    r"a `(.*)` `(.*)` machine with ubuntu-advantage-tools installed"
)


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


_detail_cache: Dict[str, Tuple[float, FeatureDetail]] = {}


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
