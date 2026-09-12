"""Build a campaign of test units directly from behave feature files.

Discovery parses the corpus with ``behave_mcp.parser``, the same parser that
selects scenarios at run time, so a campaign covers exactly the combinations
each scenario supports rather than a Cartesian product.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Iterable

from behave_mcp import parser

from .domain import CampaignError, Filters, Unit

# Above this many values, suggest close matches instead of listing them all.
_MAX_LISTED_VALUES = 15


def _feature_details(repo_root: Path) -> list[Any]:
    details = parser.discover_feature_details(repo_root)
    if not details:
        raise CampaignError(
            "no feature files found under {}".format(repo_root / "features")
        )
    return details


def available_dimensions(repo_root: Path) -> dict[str, Any]:
    """Return the releases and machine types the corpus can run."""
    dimensions = parser.aggregate_dimensions(_feature_details(repo_root))
    return {
        "releases": [
            {"name": value.name, "scenario_count": value.scenario_count}
            for value in dimensions.releases
        ],
        "machine_types": [
            {"name": value.name, "scenario_count": value.scenario_count}
            for value in dimensions.machine_types
        ],
    }


def _reject_unknown(
    values: Iterable[str], known: set[str], label: str
) -> None:
    unknown = sorted(set(values) - known)
    if not unknown:
        return

    ordered = sorted(known)
    if len(ordered) <= _MAX_LISTED_VALUES:
        hint = "known values: {}".format(", ".join(ordered))
    else:
        close: list[str] = []
        for value in unknown:
            close.extend(difflib.get_close_matches(value, ordered, n=3))
        hint = (
            "did you mean: {}".format(", ".join(dict.fromkeys(close)))
            if close
            else "{} known values".format(len(ordered))
        )
    raise CampaignError(
        "unknown {}: {}. {}".format(label, ", ".join(unknown), hint)
    )


def discover_units(repo_root: Path, filters: Filters) -> list[Unit]:
    """Return every unit in the feature corpus matching the filters."""
    details = _feature_details(repo_root)

    dimensions = parser.aggregate_dimensions(details)
    _reject_unknown(
        filters.release,
        {value.name for value in dimensions.releases},
        "release",
    )
    _reject_unknown(
        filters.machine_type,
        {value.name for value in dimensions.machine_types},
        "machine_type",
    )
    _reject_unknown(
        filters.feature, {detail.path for detail in details}, "feature"
    )

    units: list[Unit] = []
    seen: set[Unit] = set()
    for detail in details:
        for scenario in detail.scenarios:
            for combo in scenario.combos:
                unit = Unit(
                    feature=detail.path,
                    scenario=scenario.name,
                    release=combo.release,
                    machine_type=combo.machine_type,
                )
                if unit not in seen and filters.matches_unit(unit):
                    seen.add(unit)
                    units.append(unit)
    return sorted(units)
