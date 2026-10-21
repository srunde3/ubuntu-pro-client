"""Build a campaign of test units directly from behave feature files.

Discovery parses the feature files with ``behave_mcp.parser``, the same
parser that selects scenarios at run time, so a campaign covers exactly the
combinations each scenario supports rather than a Cartesian product.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Iterable

from behave_campaign.domain.records import CampaignError, Scope, Unit
from behave_mcp import parser

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
    """Return the releases and machine types the feature files can run."""
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


def discover_units(repo_root: Path, scope: Scope) -> list[Unit]:
    """Return every unit in the feature files in the scope."""
    details = _feature_details(repo_root)

    dimensions = parser.aggregate_dimensions(details)
    _reject_unknown(
        scope.release,
        {value.name for value in dimensions.releases},
        "release",
    )
    _reject_unknown(
        scope.machine_type,
        {value.name for value in dimensions.machine_types},
        "machine_type",
    )
    _reject_unknown(
        scope.feature, {detail.path for detail in details}, "feature"
    )

    # Which scenarios of its feature produce each unit. One scenario may
    # list a combo twice; two scenarios sharing a name and a combo make the
    # unit ambiguous, because behave would run them both.
    sources: dict[Unit, set[int]] = {}
    for detail in details:
        for index, scenario in enumerate(detail.scenarios):
            for combo in scenario.combos:
                unit = Unit(
                    feature=detail.path,
                    scenario=scenario.name,
                    release=combo.release,
                    machine_type=combo.machine_type,
                )
                if scope.matches_unit(unit):
                    sources.setdefault(unit, set()).add(index)

    ambiguous = sorted(
        unit for unit, found in sources.items() if len(found) > 1
    )
    if ambiguous:
        raise CampaignError(
            "{} unit(s) in scope are run by more than one scenario of the "
            "same name, so no job can name exactly one: {}. Rename those "
            "scenarios, or narrow the scope".format(
                len(ambiguous),
                "; ".join(
                    " ".join(
                        [u.feature, u.scenario, u.release, u.machine_type]
                    )
                    for u in ambiguous
                ),
            )
        )
    return sorted(sources)
