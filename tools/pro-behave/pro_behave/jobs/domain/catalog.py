"""The catalog of a checkout: its features, their scenarios and combos.

A test framework's adapter builds the catalog from its own files, and the
queries here read it. A scenario's ``type``, ``example_columns`` and
``examples`` keep the shape a Gherkin outline gives them, since
``describe_feature`` shows them as they are.
"""

import posixpath
from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Combo:
    """A ``(release, machine_type)`` pair a scenario supports."""

    release: str
    machine_type: str


@dataclass(frozen=True)
class ExamplesBlock:
    """One named set of a scenario's parameters, with its own tags."""

    name: str
    tags: list[str]
    combos: list[Combo]


@dataclass(frozen=True)
class ScenarioSummary:
    """A scenario as a caller browses and selects it.

    ``examples`` is the view per parameter set; the other fields are their
    union.
    """

    name: str
    type: str
    tags: list[str]
    requires_config: list[str]
    example_columns: list[str]
    combos: list[Combo]
    examples: list[ExamplesBlock] = field(default_factory=list)


@dataclass(frozen=True)
class FeatureDetail:
    """One feature: its path in the checkout and its scenarios."""

    path: str
    title: str
    tags: list[str]
    requires_config: list[str]
    scenarios: list[ScenarioSummary]


@dataclass(frozen=True)
class FeatureCatalogEntry:
    """A feature as a catalog listing shows it."""

    path: str
    title: str
    scenario_count: int
    requires_config: list[str]
    releases: list[str]
    machine_types: list[str]


@dataclass(frozen=True)
class DimensionValue:
    """A release or machine type, and how many scenarios support it."""

    name: str
    scenario_count: int


@dataclass(frozen=True)
class Dimensions:
    """The releases and machine types across the catalog."""

    releases: list[DimensionValue]
    machine_types: list[DimensionValue]


def normalize_feature_arg(feature: str) -> str:
    """A feature path as the catalog names it: POSIX, with no ``./``."""
    normalized = posixpath.normpath(feature.replace("\\", "/"))
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


class UnitMatch(str, Enum):
    """How a unit's key matches the scenarios of its feature."""

    FOUND = "found"
    NO_SCENARIO = "no_scenario"
    NOT_ON_COMBO = "not_on_combo"
    AMBIGUOUS = "ambiguous"


def match_unit(
    feature: FeatureDetail, scenario: str, release: str, machine_type: str
) -> UnitMatch:
    """Whether the unit's key names exactly one scenario of ``feature``.

    A unit is keyed by its scenario's name. Two scenarios may share a name
    when they cover different combos; only a combo both cover leaves the
    key naming two scenarios, so no job can select just one.
    """
    named = [s for s in feature.scenarios if s.name == scenario]
    if not named:
        return UnitMatch.NO_SCENARIO
    combo = Combo(release=release, machine_type=machine_type)
    covering = sum(1 for s in named if combo in s.combos)
    if covering == 0:
        return UnitMatch.NOT_ON_COMBO
    if covering > 1:
        return UnitMatch.AMBIGUOUS
    return UnitMatch.FOUND


def catalog_entry(feature: FeatureDetail) -> FeatureCatalogEntry:
    """A feature's listing: its scenarios' requirements and dimensions."""
    releases: list[str] = []
    machine_types: list[str] = []
    requires_config = list(feature.requires_config)
    for scenario in feature.scenarios:
        for requirement in scenario.requires_config:
            if requirement not in requires_config:
                requires_config.append(requirement)
        for combo in scenario.combos:
            if combo.release not in releases:
                releases.append(combo.release)
            if combo.machine_type not in machine_types:
                machine_types.append(combo.machine_type)
    return FeatureCatalogEntry(
        path=feature.path,
        title=feature.title,
        scenario_count=len(feature.scenarios),
        requires_config=sorted(requires_config),
        releases=sorted(releases),
        machine_types=sorted(machine_types),
    )


def filtered_combos(
    scenario: ScenarioSummary,
    release: str | None = None,
    machine_type: str | None = None,
) -> list[Combo]:
    """The scenario's combos on ``release`` and ``machine_type``."""
    return [
        combo
        for combo in scenario.combos
        if (release is None or combo.release == release)
        and (machine_type is None or combo.machine_type == machine_type)
    ]


def scenario_matches(
    scenario: ScenarioSummary,
    feature_tags: list[str],
    *,
    release: str | None = None,
    machine_type: str | None = None,
    tag: str | None = None,
    text: str | None = None,
) -> bool:
    """Whether a scenario satisfies every filter given."""
    if (release is not None or machine_type is not None) and not (
        filtered_combos(scenario, release, machine_type)
    ):
        return False
    if tag is not None and tag not in set(feature_tags) | set(scenario.tags):
        return False
    if text is not None and text.lower() not in scenario.name.lower():
        return False
    return True


def aggregate_dimensions(features: list[FeatureDetail]) -> Dimensions:
    """Every release and machine type, with how many scenarios support it."""
    release_counts: dict[str, int] = {}
    machine_type_counts: dict[str, int] = {}
    for feature in features:
        for scenario in feature.scenarios:
            for release in {combo.release for combo in scenario.combos}:
                release_counts[release] = release_counts.get(release, 0) + 1
            for machine_type in {
                combo.machine_type for combo in scenario.combos
            }:
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
