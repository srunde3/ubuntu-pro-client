"""The catalog's queries, and how a unit's key matches its scenarios."""

import dataclasses

import pytest

from pro_behave.jobs.domain import catalog


def test_catalog_entry_aggregates_scenarios():
    detail = catalog.FeatureDetail(
        path="features/cli/attach.feature",
        title="CLI attach",
        tags=["uses.config.contract_token"],
        requires_config=["contract_token"],
        scenarios=[
            catalog.ScenarioSummary(
                name="Attach on a machine",
                type="scenario_outline",
                tags=[],
                requires_config=["contract_token"],
                example_columns=[],
                combos=[
                    catalog.Combo(release="resolute", machine_type="lxd-vm"),
                    catalog.Combo(
                        release="jammy", machine_type="lxd-container"
                    ),
                ],
            ),
            catalog.ScenarioSummary(
                name="Attach invalid token",
                type="scenario_outline",
                tags=[],
                requires_config=["contract_token_staging_expired"],
                example_columns=[],
                combos=[
                    catalog.Combo(
                        release="jammy", machine_type="lxd-container"
                    ),
                ],
            ),
        ],
    )
    assert dataclasses.asdict(catalog.catalog_entry(detail)) == {
        "path": "features/cli/attach.feature",
        "title": "CLI attach",
        "scenario_count": 2,
        "requires_config": [
            "contract_token",
            "contract_token_staging_expired",
        ],
        "releases": ["jammy", "resolute"],
        "machine_types": ["lxd-container", "lxd-vm"],
    }


def test_scenario_matches_filters():
    scenario = catalog.ScenarioSummary(
        name="Attach on a machine",
        type="scenario",
        tags=["arm64"],
        requires_config=[],
        example_columns=[],
        combos=[
            catalog.Combo(release="jammy", machine_type="lxd-container"),
            catalog.Combo(release="resolute", machine_type="lxd-vm"),
        ],
    )
    assert catalog.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-vm"
    )
    assert not catalog.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-container"
    )
    assert catalog.scenario_matches(scenario, [], tag="arm64")


def test_aggregate_dimensions_counts_scenarios_once_per_value():
    details = [
        catalog.FeatureDetail(
            path="features/cli/attach.feature",
            title="CLI attach",
            tags=[],
            requires_config=[],
            scenarios=[
                catalog.ScenarioSummary(
                    name="Attach on a machine",
                    type="scenario",
                    tags=[],
                    requires_config=[],
                    example_columns=[],
                    combos=[
                        catalog.Combo(
                            release="jammy", machine_type="lxd-container"
                        ),
                        catalog.Combo(release="jammy", machine_type="lxd-vm"),
                    ],
                ),
                catalog.ScenarioSummary(
                    name="Detach",
                    type="scenario",
                    tags=[],
                    requires_config=[],
                    example_columns=[],
                    combos=[
                        catalog.Combo(
                            release="resolute", machine_type="lxd-vm"
                        ),
                    ],
                ),
            ],
        )
    ]
    dimensions = dataclasses.asdict(catalog.aggregate_dimensions(details))
    assert dimensions["releases"] == [
        {"name": "jammy", "scenario_count": 1},
        {"name": "resolute", "scenario_count": 1},
    ]
    assert dimensions["machine_types"] == [
        {"name": "lxd-container", "scenario_count": 1},
        {"name": "lxd-vm", "scenario_count": 2},
    ]


FEATURE = "features/cli/attach.feature"
SCENARIO = "Attach command"
OTHER_SCENARIO = "Detach command"
JAMMY_LXD = ("jammy", "lxd-container")
NOBLE_VM = ("noble", "lxd-vm")


UNIT_MATCH_CASES = [
    pytest.param(
        [(SCENARIO, {JAMMY_LXD})], catalog.UnitMatch.FOUND, id="one-scenario"
    ),
    pytest.param(
        [(SCENARIO, {JAMMY_LXD}), (SCENARIO, {NOBLE_VM})],
        catalog.UnitMatch.FOUND,
        id="a-shared-name-on-other-combos",
    ),
    pytest.param(
        [(OTHER_SCENARIO, {JAMMY_LXD})],
        catalog.UnitMatch.NO_SCENARIO,
        id="no-such-name",
    ),
    pytest.param(
        [(SCENARIO, {NOBLE_VM})],
        catalog.UnitMatch.NOT_ON_COMBO,
        id="not-on-this-combo",
    ),
    pytest.param(
        [(SCENARIO, {JAMMY_LXD}), (SCENARIO, {JAMMY_LXD, NOBLE_VM})],
        catalog.UnitMatch.AMBIGUOUS,
        id="two-scenarios-on-one-combo",
    ),
]


def test_every_unit_match_is_reached_by_a_case():
    assert {case.values[1] for case in UNIT_MATCH_CASES} == set(
        catalog.UnitMatch
    )


def feature_of(scenarios):
    """A feature holding a scenario for each ``(name, combos)`` pair."""
    return catalog.FeatureDetail(
        path=FEATURE,
        title="",
        tags=[],
        requires_config=[],
        scenarios=[
            catalog.ScenarioSummary(
                name=name,
                type="scenario_outline",
                tags=[],
                requires_config=[],
                example_columns=[],
                combos=[catalog.Combo(*combo) for combo in sorted(combos)],
            )
            for name, combos in scenarios
        ],
    )


@pytest.mark.parametrize("scenarios, expected", UNIT_MATCH_CASES)
def test_match_unit(scenarios, expected):
    release, machine_type = JAMMY_LXD

    match = catalog.match_unit(
        feature_of(scenarios), SCENARIO, release, machine_type
    )

    assert match == expected
