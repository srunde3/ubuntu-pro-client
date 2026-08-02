"""Unit tests for ``features/behave_features.py``.

This module is the repo's authority on what a ``.feature`` file means
(Examples table shape, config-requirement tags, machine_type vocabulary),
consumed by both ``tools/coverage_gaps.py`` and the behave MCP server
(``tools/mcp-behave-server``) -- see the module docstring. Tests here cover
both the behave-free projection/filtering logic (using lightweight fakes
that duck-type the parsed behave model, so no real parsing is needed) and
the real filesystem reader (``discover_feature_files``/
``discover_feature_details``, which do parse real files via ``tmp_path``
fixtures).
"""

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
    def __init__(self, cells):
        self.cells = cells


class _Example:
    def __init__(self, headings, rows):
        self.table = _Table(headings, rows)


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


# ---- requires_config_from_tags ----


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


def test_requires_config_from_tags_ignores_non_config_tags():
    assert behave_features.requires_config_from_tags(["slow", "arm64"]) == []


# ---- combos_from_scenario ----


def test_combos_from_outline_examples():
    scenario = _outline(
        "Attach",
        ["release", "machine_type"],
        [["jammy", "lxd-container"], ["resolute", "lxd-vm"]],
    )
    combos = behave_features.combos_from_scenario(scenario)
    assert _combo_dicts(combos) == [
        {"release": "jammy", "machine_type": "lxd-container"},
        {"release": "resolute", "machine_type": "lxd-vm"},
    ]


def test_combos_deduplicates_and_ignores_extra_columns():
    scenario = _outline(
        "Attach",
        ["release", "machine_type", "note"],
        [
            ["jammy", "lxd-container", "a"],
            ["jammy", "lxd-container", "b"],
        ],
    )
    combos = behave_features.combos_from_scenario(scenario)
    assert _combo_dicts(combos) == [
        {"release": "jammy", "machine_type": "lxd-container"}
    ]


def test_combos_from_hardcoded_step_without_examples():
    scenario = _Scenario(
        "Plain",
        "scenario",
        [],
        [
            "a `jammy` `lxd-container` machine with"
            " ubuntu-advantage-tools installed"
        ],
    )
    combos = behave_features.combos_from_scenario(scenario)
    assert _combo_dicts(combos) == [
        {"release": "jammy", "machine_type": "lxd-container"}
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
    # Literal release from the step wins; machine_type still from the row.
    combos = behave_features.combos_from_scenario(scenario)
    assert _combo_dicts(combos) == [
        {"release": "jammy", "machine_type": "lxd-vm"}
    ]


def test_combos_skips_placeholder_only_rows():
    scenario = _outline(
        "Empty",
        ["release", "machine_type"],
        [],
    )
    assert behave_features.combos_from_scenario(scenario) == []


# ---- summarize_feature ----


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
    assert scenario["name"] == "Attach"
    assert scenario["type"] == "scenario_outline"
    assert scenario["tags"] == ["arm64"]
    # Feature-level config tag propagates to the scenario requirement.
    assert scenario["requires_config"] == ["contract_token"]
    assert scenario["example_columns"] == [
        "release",
        "machine_type",
        "landscape",
    ]
    assert scenario["combos"] == [
        {"release": "jammy", "machine_type": "lxd-container"}
    ]


# ---- catalog_entry ----


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
    entry = behave_features.catalog_entry(detail)
    assert dataclasses.asdict(entry) == {
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


# ---- filtered_combos / scenario_matches ----


def _scenario_summary():
    return behave_features.ScenarioSummary(
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


def test_filtered_combos_by_release_and_machine_type():
    scenario = _scenario_summary()
    combos = behave_features.filtered_combos(scenario, "resolute", None)
    assert _combo_dicts(combos) == [
        {"release": "resolute", "machine_type": "lxd-vm"}
    ]
    assert (
        behave_features.filtered_combos(scenario, "resolute", "lxd-container")
        == []
    )


def test_scenario_matches_combo_filter():
    scenario = _scenario_summary()
    assert behave_features.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-vm"
    )
    assert not behave_features.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-container"
    )


def test_scenario_matches_tag_from_feature_or_scenario():
    scenario = _scenario_summary()
    assert behave_features.scenario_matches(scenario, [], tag="arm64")
    assert behave_features.scenario_matches(
        scenario,
        ["uses.config.contract_token"],
        tag="uses.config.contract_token",
    )
    assert not behave_features.scenario_matches(scenario, [], tag="slow")


def test_scenario_matches_text_is_case_insensitive_substring():
    scenario = _scenario_summary()
    assert behave_features.scenario_matches(scenario, [], text="MACHINE")
    assert not behave_features.scenario_matches(scenario, [], text="detach")


def test_scenario_matches_no_filters_is_true():
    assert behave_features.scenario_matches(_scenario_summary(), [])


# ---- aggregate_dimensions ----


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


# ---- discover_feature_files / discover_feature_details (real parsing) ----

_SAMPLE_FEATURE = """\
@uses.config.contract_token
Feature: Sample feature

  Scenario Outline: Attach on a machine
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
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


def test_discover_feature_files_missing_dir(tmp_path):
    assert behave_features.discover_feature_files(tmp_path) == []


def test_discover_feature_details_parses_scenarios(tmp_path):
    (tmp_path / "features" / "cli").mkdir(parents=True)
    (tmp_path / "features" / "cli" / "sample.feature").write_text(
        _SAMPLE_FEATURE, encoding="utf-8"
    )

    details = behave_features.discover_feature_details(tmp_path)

    assert len(details) == 1
    detail = details[0]
    assert detail.path == "features/cli/sample.feature"
    assert detail.title == "Sample feature"
    assert detail.requires_config == ["contract_token"]
    scenario = detail.scenarios[0]
    assert _combo_dicts(scenario.combos) == [
        {"release": "jammy", "machine_type": "lxd-container"},
        {"release": "resolute", "machine_type": "lxd-vm"},
    ]


def test_discover_feature_details_skips_unparseable(tmp_path):
    (tmp_path / "features").mkdir(parents=True)
    (tmp_path / "features" / "broken.feature").write_text(
        "This is not gherkin: {[}\nScenario without feature\n",
        encoding="utf-8",
    )

    # Must not raise; the broken file is simply omitted.
    assert behave_features.discover_feature_details(tmp_path) == []


def test_discover_feature_details_missing_dir(tmp_path):
    assert behave_features.discover_feature_details(tmp_path) == []


def test_discover_feature_details_uses_mtime_cache(tmp_path):
    features_dir = tmp_path / "features"
    features_dir.mkdir(parents=True)
    feature_path = features_dir / "sample.feature"
    feature_path.write_text(_SAMPLE_FEATURE, encoding="utf-8")

    first = behave_features.discover_feature_details(tmp_path)
    assert first[0].title == "Sample feature"

    # Same mtime -> cached result returned even if content changes underneath.
    stat = feature_path.stat()
    feature_path.write_text("Feature: Changed\n", encoding="utf-8")
    os.utime(feature_path, (stat.st_atime, stat.st_mtime))
    cached = behave_features.discover_feature_details(tmp_path)
    assert cached[0].title == "Sample feature"
