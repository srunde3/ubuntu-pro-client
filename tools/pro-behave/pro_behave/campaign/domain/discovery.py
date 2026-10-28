"""The units a campaign covers, from the scenarios a checkout's catalog offers.

The catalog is the one the job service validates units against, so a
campaign covers exactly the combinations each scenario supports rather than
a Cartesian product.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Iterable, Sequence

from pro_behave.campaign.domain.records import CampaignError, Scope, Unit

# Above this many values, suggest close matches instead of listing them all.
_MAX_LISTED_VALUES = 15


@dataclass(frozen=True)
class CatalogScenario:
    """One scenario of a feature, and the ``(release, machine_type)``
    combos it supports."""

    feature: str
    scenario: str
    combos: tuple[tuple[str, str], ...]


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


def discover_units(
    features: Sequence[str],
    scenarios: Sequence[CatalogScenario],
    scope: Scope,
) -> list[Unit]:
    """Every unit the catalog offers in ``scope``.

    Raises CampaignError for a release, machine type or feature the catalog
    does not have, rather than silently matching nothing.
    """
    _reject_unknown(
        scope.release,
        {release for s in scenarios for release, _ in s.combos},
        "release",
    )
    _reject_unknown(
        scope.machine_type,
        {machine_type for s in scenarios for _, machine_type in s.combos},
        "machine_type",
    )
    _reject_unknown(scope.feature, set(features), "feature")

    # Which scenarios offer each unit. A unit is keyed by its scenario's
    # name, so two scenarios sharing a name and a combo leave a key that
    # names both; one scenario listing a combo twice is still one.
    sources: dict[Unit, set[int]] = {}
    for index, scenario in enumerate(scenarios):
        for release, machine_type in scenario.combos:
            unit = Unit(
                feature=scenario.feature,
                scenario=scenario.scenario,
                release=release,
                machine_type=machine_type,
            )
            if scope.matches_unit(unit):
                sources.setdefault(unit, set()).add(index)

    ambiguous = sorted(
        unit for unit, found in sources.items() if len(found) > 1
    )
    if ambiguous:
        raise CampaignError(
            "{} unit(s) in scope are named by more than one scenario of the "
            "same name, so no job can select exactly one: {}. Rename those "
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
