"""behave as the test framework: the catalog it reads, mapped for the tool."""

from pro_e2e.behave import parser
from pro_e2e.behave.framework import BehaveFramework, to_feature
from pro_e2e.jobs.domain import catalog
from pro_e2e.jobs.domain.jobs import Unit
from pro_e2e.jobs.messages import Instance

SAMPLE_FEATURE = """\
@uses.config.contract_token
Feature: Sample feature

  Scenario Outline: Attach on a machine
    Given a `<release>` `<machine_type>` machine with \
ubuntu-advantage-tools installed
    When I attach

    Examples: ubuntu release
      | release  | machine_type  |
      | jammy    | lxd-container |
      | resolute | lxd-vm        |
"""


def test_the_catalog_is_the_parsed_feature_files(tmp_path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "sample.feature").write_text(SAMPLE_FEATURE)

    features = BehaveFramework().catalog(tmp_path)

    assert [feature.path for feature in features] == [
        "features/sample.feature"
    ]
    assert features[0].title == "Sample feature"
    assert [s.name for s in features[0].scenarios] == ["Attach on a machine"]


def test_every_field_of_a_parsed_feature_reaches_the_catalog():
    parsed = parser.FeatureDetail(
        path="features/cli/attach.feature",
        title="CLI attach",
        tags=["uses.config.contract_token"],
        requires_config=["contract_token"],
        scenarios=[
            parser.ScenarioSummary(
                name="Attach on a machine",
                type="scenario_outline",
                tags=["slow"],
                requires_config=["contract_token"],
                example_columns=["release", "machine_type"],
                combos=[parser.Combo("jammy", "lxd-container")],
                examples=[
                    parser.ExamplesBlock(
                        name="ubuntu release",
                        tags=["arm64"],
                        combos=[parser.Combo("jammy", "lxd-container")],
                    )
                ],
            )
        ],
    )

    mapped = to_feature(parsed)

    assert mapped == catalog.FeatureDetail(
        path="features/cli/attach.feature",
        title="CLI attach",
        tags=["uses.config.contract_token"],
        requires_config=["contract_token"],
        scenarios=[
            catalog.ScenarioSummary(
                name="Attach on a machine",
                type="scenario_outline",
                tags=["slow"],
                requires_config=["contract_token"],
                example_columns=["release", "machine_type"],
                combos=[catalog.Combo("jammy", "lxd-container")],
                examples=[
                    catalog.ExamplesBlock(
                        name="ubuntu release",
                        tags=["arm64"],
                        combos=[catalog.Combo("jammy", "lxd-container")],
                    )
                ],
            )
        ],
    )


def test_the_invocation_names_the_unit_s_own_instances(tmp_path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "sample.feature").write_text(SAMPLE_FEATURE)
    unit = Unit(
        "./features/sample.feature",
        "Attach on a machine",
        "jammy",
        "lxd-container",
    )

    invocation = BehaveFramework().invocation(
        tmp_path, unit, tmp_path / "report.json"
    )

    assert invocation.instances == [
        Instance(
            id="features/sample.feature:10",
            name="Attach on a machine -- @1.1 ubuntu release",
        )
    ]
    assert invocation.command[:5] == [
        "tox",
        "-e",
        "behave",
        "--",
        "./features/sample.feature",
    ]
