"""Unit tests for ``features.behave_features``."""

import dataclasses
import os

from features import behave_features


class _Step:
    def __init__(self, name):
        self.name = name


class _Table:
    def __init__(self, headings, rows):
        self.headings = headings
        self.rows = [_Row(cells) for cells in rows]


class _Row:
    def __init__(self, cells, line=None):
        self.cells = cells
        self.line = line


class _Example:
    def __init__(self, headings, rows, tags=None, name=""):
        self.table = _Table(headings, rows)
        self.tags = tags or []
        self.name = name


class _Scenario:
    def __init__(self, name, type_, tags, steps, examples=None):
        self.name = name
        self.type = type_
        self.tags = tags
        self.steps = [_Step(step) for step in steps]
        self.examples = examples or []


class _Feature:
    def __init__(self, name, tags, scenarios):
        self.name = name
        self.tags = tags
        self.scenarios = scenarios


_MACHINE_STEP = (
    "a `<release>` `<machine_type>` machine with"
    " ubuntu-advantage-tools installed"
)


def _outline(name, headings, rows, tags=None):
    return _Scenario(
        name,
        "scenario_outline",
        tags or [],
        [_MACHINE_STEP, "When I attach"],
        [_Example(headings, rows)],
    )


def _combo_dicts(combos):
    return [dataclasses.asdict(combo) for combo in combos]


def test_requires_config_from_tags_extracts_and_sorts():
    tags = [
        "uses.config.contract_token",
        "arm64",
        "uses.config.contract_token_staging_expired",
    ]
    assert behave_features.requires_config_from_tags(tags) == [
        "contract_token",
        "contract_token_staging_expired",
    ]


def test_combos_from_outline_examples():
    scenario = _outline(
        "Attach",
        ["release", "machine_type"],
        [["jammy", "lxd-container"], ["resolute", "lxd-vm"]],
    )
    assert _combo_dicts(behave_features.combos_from_scenario(scenario)) == [
        {"release": "jammy", "machine_type": "lxd-container"},
        {"release": "resolute", "machine_type": "lxd-vm"},
    ]


def test_hardcoded_step_overrides_example_release():
    scenario = _Scenario(
        "Override",
        "scenario_outline",
        [],
        [
            "a `jammy` `<machine_type>` machine with"
            " ubuntu-advantage-tools installed"
        ],
        [_Example(["release", "machine_type"], [["noble", "lxd-vm"]])],
    )
    assert _combo_dicts(behave_features.combos_from_scenario(scenario)) == [
        {"release": "jammy", "machine_type": "lxd-vm"}
    ]


def test_examples_blocks_from_scenario_carries_per_block_tags():
    scenario = _Scenario(
        "Check pro version",
        "scenario_outline",
        [],
        [_MACHINE_STEP],
        [
            _Example(
                ["release", "machine_type"],
                [["jammy", "lxd-container"]],
                tags=["releases:lts_supported"],
                name="standard",
            ),
            _Example(
                ["release", "machine_type"],
                [["jammy", "aws.pro"]],
                tags=["releases:lts_esm"],
                name="clouds",
            ),
        ],
    )
    blocks = behave_features.examples_blocks_from_scenario(scenario)
    assert [b.name for b in blocks] == ["standard", "clouds"]
    assert [b.tags for b in blocks] == [
        ["releases:lts_supported"],
        ["releases:lts_esm"],
    ]
    assert _combo_dicts(blocks[0].combos) == [
        {"release": "jammy", "machine_type": "lxd-container"}
    ]
    assert _combo_dicts(blocks[1].combos) == [
        {"release": "jammy", "machine_type": "aws.pro"}
    ]


def test_summarize_feature_shapes_scenarios():
    feature = _Feature(
        "CLI attach",
        ["uses.config.contract_token"],
        [
            _outline(
                "Attach",
                ["release", "machine_type", "landscape"],
                [["jammy", "lxd-container", "disabled"]],
                tags=["arm64"],
            )
        ],
    )
    summary = dataclasses.asdict(behave_features.summarize_feature(feature))
    assert summary["title"] == "CLI attach"
    assert summary["requires_config"] == ["contract_token"]
    scenario = summary["scenarios"][0]
    assert scenario["tags"] == ["arm64"]
    assert scenario["requires_config"] == ["contract_token"]
    assert scenario["example_columns"] == [
        "release",
        "machine_type",
        "landscape",
    ]
    assert scenario["combos"] == [
        {"release": "jammy", "machine_type": "lxd-container"}
    ]
    assert scenario["examples"][0]["combos"] == [
        {"release": "jammy", "machine_type": "lxd-container"}
    ]


def test_catalog_entry_aggregates_scenarios():
    detail = behave_features.FeatureDetail(
        path="features/cli/attach.feature",
        title="CLI attach",
        tags=["uses.config.contract_token"],
        requires_config=["contract_token"],
        scenarios=[
            behave_features.ScenarioSummary(
                name="Attach on a machine",
                type="scenario_outline",
                tags=[],
                requires_config=["contract_token"],
                example_columns=[],
                combos=[
                    behave_features.Combo(
                        release="resolute", machine_type="lxd-vm"
                    ),
                    behave_features.Combo(
                        release="jammy", machine_type="lxd-container"
                    ),
                ],
            ),
            behave_features.ScenarioSummary(
                name="Attach invalid token",
                type="scenario_outline",
                tags=[],
                requires_config=["contract_token_staging_expired"],
                example_columns=[],
                combos=[
                    behave_features.Combo(
                        release="jammy", machine_type="lxd-container"
                    ),
                ],
            ),
        ],
    )
    assert dataclasses.asdict(behave_features.catalog_entry(detail)) == {
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
    scenario = behave_features.ScenarioSummary(
        name="Attach on a machine",
        type="scenario",
        tags=["arm64"],
        requires_config=[],
        example_columns=[],
        combos=[
            behave_features.Combo(
                release="jammy", machine_type="lxd-container"
            ),
            behave_features.Combo(release="resolute", machine_type="lxd-vm"),
        ],
    )
    assert behave_features.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-vm"
    )
    assert not behave_features.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-container"
    )
    assert behave_features.scenario_matches(scenario, [], tag="arm64")
    assert behave_features.scenario_matches(scenario, [], text="MACHINE")


def test_aggregate_dimensions_counts_scenarios_once_per_value():
    details = [
        behave_features.FeatureDetail(
            path="features/cli/attach.feature",
            title="CLI attach",
            tags=[],
            requires_config=[],
            scenarios=[
                behave_features.ScenarioSummary(
                    name="Attach on a machine",
                    type="scenario",
                    tags=[],
                    requires_config=[],
                    example_columns=[],
                    combos=[
                        behave_features.Combo(
                            release="jammy", machine_type="lxd-container"
                        ),
                        behave_features.Combo(
                            release="jammy", machine_type="lxd-vm"
                        ),
                    ],
                ),
                behave_features.ScenarioSummary(
                    name="Detach",
                    type="scenario",
                    tags=[],
                    requires_config=[],
                    example_columns=[],
                    combos=[
                        behave_features.Combo(
                            release="resolute", machine_type="lxd-vm"
                        ),
                    ],
                ),
            ],
        )
    ]
    dimensions = dataclasses.asdict(
        behave_features.aggregate_dimensions(details)
    )
    assert dimensions["releases"] == [
        {"name": "jammy", "scenario_count": 1},
        {"name": "resolute", "scenario_count": 1},
    ]
    assert dimensions["machine_types"] == [
        {"name": "lxd-container", "scenario_count": 1},
        {"name": "lxd-vm", "scenario_count": 2},
    ]


_SAMPLE_FEATURE = """\
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


def test_discover_feature_files_sorted(tmp_path):
    (tmp_path / "features" / "cli").mkdir(parents=True)
    (tmp_path / "features" / "b.feature").write_text("", encoding="utf-8")
    (tmp_path / "features" / "cli" / "a.feature").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "features" / "notes.txt").write_text("", encoding="utf-8")

    assert behave_features.discover_feature_files(tmp_path) == [
        "features/b.feature",
        "features/cli/a.feature",
    ]


def test_discover_feature_details_parses_scenarios(tmp_path):
    (tmp_path / "features" / "cli").mkdir(parents=True)
    (tmp_path / "features" / "cli" / "sample.feature").write_text(
        _SAMPLE_FEATURE, encoding="utf-8"
    )

    details = behave_features.discover_feature_details(tmp_path)

    assert len(details) == 1
    assert details[0].path == "features/cli/sample.feature"
    assert details[0].title == "Sample feature"
    assert _combo_dicts(details[0].scenarios[0].combos) == [
        {"release": "jammy", "machine_type": "lxd-container"},
        {"release": "resolute", "machine_type": "lxd-vm"},
    ]


def test_discover_feature_details_skips_unparseable(tmp_path):
    (tmp_path / "features").mkdir(parents=True)
    (tmp_path / "features" / "broken.feature").write_text(
        "This is not gherkin: {[}\nScenario without feature\n",
        encoding="utf-8",
    )

    assert behave_features.discover_feature_details(tmp_path) == []


def test_discover_feature_details_uses_mtime_cache(tmp_path):
    features_dir = tmp_path / "features"
    features_dir.mkdir(parents=True)
    feature_path = features_dir / "sample.feature"
    feature_path.write_text(_SAMPLE_FEATURE, encoding="utf-8")

    first = behave_features.discover_feature_details(tmp_path)
    assert first[0].title == "Sample feature"

    stat = feature_path.stat()
    feature_path.write_text("Feature: Changed\n", encoding="utf-8")
    os.utime(feature_path, (stat.st_atime, stat.st_mtime))
    cached = behave_features.discover_feature_details(tmp_path)
    assert cached[0].title == "Sample feature"
