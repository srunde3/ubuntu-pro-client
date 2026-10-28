"""The catalog's types as the DTOs the job service returns."""

from pro_behave.jobs.domain import catalog
from pro_behave.jobs.messages import (
    Combo,
    Dimensions,
    DimensionValue,
    ExamplesBlock,
    FeatureCatalogEntry,
    ScenarioSummary,
)


def to_combo(combo: catalog.Combo) -> Combo:
    return Combo(release=combo.release, machine_type=combo.machine_type)


def to_examples_block(block: catalog.ExamplesBlock) -> ExamplesBlock:
    return ExamplesBlock(
        name=block.name,
        tags=list(block.tags),
        combos=[to_combo(combo) for combo in block.combos],
    )


def to_scenario_summary(
    scenario: catalog.ScenarioSummary,
) -> ScenarioSummary:
    return ScenarioSummary(
        name=scenario.name,
        type=scenario.type,
        tags=list(scenario.tags),
        requires_config=list(scenario.requires_config),
        example_columns=list(scenario.example_columns),
        combos=[to_combo(combo) for combo in scenario.combos],
        examples=[to_examples_block(block) for block in scenario.examples],
    )


def to_catalog_entry(
    entry: catalog.FeatureCatalogEntry,
) -> FeatureCatalogEntry:
    return FeatureCatalogEntry(
        path=entry.path,
        title=entry.title,
        scenario_count=entry.scenario_count,
        requires_config=list(entry.requires_config),
        releases=list(entry.releases),
        machine_types=list(entry.machine_types),
    )


def to_dimensions(dimensions: catalog.Dimensions) -> Dimensions:
    return Dimensions(
        releases=[
            DimensionValue(
                name=value.name, scenario_count=value.scenario_count
            )
            for value in dimensions.releases
        ],
        machine_types=[
            DimensionValue(
                name=value.name, scenario_count=value.scenario_count
            )
            for value in dimensions.machine_types
        ],
    )
