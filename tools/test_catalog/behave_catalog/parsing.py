"""Parse behave feature files into typed records for the test catalog.

Combo derivation is structural, not tag-driven: a scenario's valid
``(release, machine_type)`` pairs come from its ``Examples:`` table columns,
or -- for scenarios without an Examples table -- from a literal override in
the first ``Given`` step's text. This mirrors what
``behave -D releases=... -D machine_types=...`` will actually execute, so
the catalog never advertises a combo that isn't really runnable.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from behave.parser import parse_file

_CONFIG_TAG_PREFIX = "uses.config."
_GIVEN_MACHINE_STEP_RE = re.compile(
    r"a `(.*)` `(.*)` machine with ubuntu-advantage-tools installed"
)


@dataclass(frozen=True)
class Combo:
    """A single valid ``(release, machine_type)`` pair for a scenario."""

    release: str
    machine_type: str


@dataclass(frozen=True)
class ScenarioRecord:
    """A scenario's browse/select metadata, ready to load into the catalog."""

    name: str
    type: str
    tags: tuple[str, ...]
    requires_config: tuple[str, ...]
    example_columns: tuple[str, ...]
    combos: tuple[Combo, ...]
    document: str


@dataclass(frozen=True)
class FeatureRecord:
    """Full parsed metadata for one feature file."""

    path: str
    title: str
    tags: tuple[str, ...]
    requires_config: tuple[str, ...]
    scenarios: tuple[ScenarioRecord, ...]


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _literal_or_none(value: str) -> Optional[str]:
    if not value or _is_placeholder(value):
        return None
    return value


def _requires_config_from_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    """Return sorted config requirements from ``uses.config.*`` tags."""
    requirements: list[str] = []
    for tag in tags:
        if tag.startswith(_CONFIG_TAG_PREFIX):
            remainder = tag[len(_CONFIG_TAG_PREFIX) :]
            if remainder and remainder not in requirements:
                requirements.append(remainder)
    return tuple(sorted(requirements))


def _step_machine_override(
    scenario: Any,
) -> tuple[Optional[str], Optional[str]]:
    steps = getattr(scenario, "steps", None) or []
    if not steps:
        return None, None
    match = _GIVEN_MACHINE_STEP_RE.match(getattr(steps[0], "name", "") or "")
    if not match:
        return None, None
    return _literal_or_none(match.group(1)), _literal_or_none(match.group(2))


def _add_combo(
    combos: list[Combo],
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
) -> list[Combo]:
    combos: list[Combo] = []
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


def _combos_from_scenario(scenario: Any) -> list[Combo]:
    """Return distinct combos for a scenario, unioned across examples."""
    step_release, step_machine_type = _step_machine_override(scenario)
    combos: list[Combo] = []
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


def _example_columns(scenario: Any) -> tuple[str, ...]:
    columns: list[str] = []
    for example in getattr(scenario, "examples", None) or []:
        table = getattr(example, "table", None)
        if table is None:
            continue
        for heading in getattr(table, "headings", []) or []:
            if heading not in columns:
                columns.append(heading)
    return tuple(columns)


def _step_texts(scenario: Any) -> list[str]:
    return [
        str(getattr(step, "name", "") or "")
        for step in getattr(scenario, "steps", None) or []
    ]


def _document(
    feature_title: str,
    scenario_name: str,
    tags: tuple[str, ...],
    steps: list[str],
) -> str:
    """Flatten a scenario's searchable text into one document.

    Feeds the FTS index behind keyword queries like "apt" or "security" --
    it deliberately includes step text, not just names/tags, since that is
    often where the actual subject matter (a command, a file, a service)
    appears.
    """
    parts = [feature_title, scenario_name, *tags, *steps]
    return "\n".join(part for part in parts if part)


def summarize_feature(feature: Any, path: str) -> FeatureRecord:
    """Turn a parsed behave ``Feature`` into a ``FeatureRecord``."""
    feature_tags = tuple(
        str(tag) for tag in (getattr(feature, "tags", []) or [])
    )
    feature_title = getattr(feature, "name", "") or ""
    scenarios: list[ScenarioRecord] = []
    for scenario in getattr(feature, "scenarios", []) or []:
        scenario_tags = tuple(
            str(tag) for tag in (getattr(scenario, "tags", []) or [])
        )
        effective_tags = feature_tags + scenario_tags
        scenario_name = getattr(scenario, "name", "") or ""
        scenarios.append(
            ScenarioRecord(
                name=scenario_name,
                type=getattr(scenario, "type", "scenario"),
                tags=scenario_tags,
                requires_config=_requires_config_from_tags(effective_tags),
                example_columns=_example_columns(scenario),
                combos=tuple(_combos_from_scenario(scenario)),
                document=_document(
                    feature_title,
                    scenario_name,
                    effective_tags,
                    _step_texts(scenario),
                ),
            )
        )
    return FeatureRecord(
        path=path,
        title=feature_title,
        tags=feature_tags,
        requires_config=_requires_config_from_tags(feature_tags),
        scenarios=tuple(scenarios),
    )


def parse_feature_file(
    path: Path, repo_relative_path: str
) -> Optional[FeatureRecord]:
    """Parse one ``.feature`` file, returning ``None`` if it fails to parse."""
    try:
        feature = parse_file(str(path))
    except Exception:
        return None
    if feature is None:
        return None
    return summarize_feature(feature, repo_relative_path)
