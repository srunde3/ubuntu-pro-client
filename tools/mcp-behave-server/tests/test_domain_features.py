"""Unit tests for the behave-free feature introspection helpers in ``domain``.

These use lightweight fakes that duck-type the behave model objects, so the
projection and filtering logic can be verified without parsing real files.
"""

from behave_mcp import domain


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


# ---- requires_config_from_tags ----


def test_requires_config_from_tags_extracts_and_sorts():
    tags = [
        "uses.config.contract_token",
        "arm64",
        "uses.config.contract_token_staging_expired",
    ]
    assert domain.requires_config_from_tags(tags) == [
        "contract_token",
        "contract_token_staging_expired",
    ]


def test_requires_config_from_tags_ignores_non_config_tags():
    assert domain.requires_config_from_tags(["slow", "arm64"]) == []


# ---- combos_from_scenario ----


def test_combos_from_outline_examples():
    scenario = _outline(
        "Attach",
        ["release", "machine_type"],
        [["jammy", "lxd-container"], ["resolute", "lxd-vm"]],
    )
    assert domain.combos_from_scenario(scenario) == [
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
    assert domain.combos_from_scenario(scenario) == [
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
    assert domain.combos_from_scenario(scenario) == [
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
    assert domain.combos_from_scenario(scenario) == [
        {"release": "jammy", "machine_type": "lxd-vm"}
    ]


def test_combos_skips_placeholder_only_rows():
    scenario = _outline(
        "Empty",
        ["release", "machine_type"],
        [],
    )
    assert domain.combos_from_scenario(scenario) == []


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
    summary = domain.summarize_feature(feature)
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
    detail = {
        "path": "features/cli/attach.feature",
        "title": "CLI attach",
        "tags": ["uses.config.contract_token"],
        "requires_config": ["contract_token"],
        "scenarios": [
            {
                "requires_config": ["contract_token"],
                "combos": [
                    {"release": "resolute", "machine_type": "lxd-vm"},
                    {"release": "jammy", "machine_type": "lxd-container"},
                ],
            },
            {
                "requires_config": ["contract_token_staging_expired"],
                "combos": [
                    {"release": "jammy", "machine_type": "lxd-container"}
                ],
            },
        ],
    }
    entry = domain.catalog_entry(detail)
    assert entry == {
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
    return {
        "name": "Attach on a machine",
        "tags": ["arm64"],
        "combos": [
            {"release": "jammy", "machine_type": "lxd-container"},
            {"release": "resolute", "machine_type": "lxd-vm"},
        ],
    }


def test_filtered_combos_by_release_and_machine_type():
    scenario = _scenario_summary()
    assert domain.filtered_combos(scenario, "resolute", None) == [
        {"release": "resolute", "machine_type": "lxd-vm"}
    ]
    assert domain.filtered_combos(scenario, "resolute", "lxd-container") == []


def test_scenario_matches_combo_filter():
    scenario = _scenario_summary()
    assert domain.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-vm"
    )
    assert not domain.scenario_matches(
        scenario, [], release="resolute", machine_type="lxd-container"
    )


def test_scenario_matches_tag_from_feature_or_scenario():
    scenario = _scenario_summary()
    assert domain.scenario_matches(scenario, [], tag="arm64")
    assert domain.scenario_matches(
        scenario,
        ["uses.config.contract_token"],
        tag="uses.config.contract_token",
    )
    assert not domain.scenario_matches(scenario, [], tag="slow")


def test_scenario_matches_text_is_case_insensitive_substring():
    scenario = _scenario_summary()
    assert domain.scenario_matches(scenario, [], text="MACHINE")
    assert not domain.scenario_matches(scenario, [], text="detach")


def test_scenario_matches_no_filters_is_true():
    assert domain.scenario_matches(_scenario_summary(), [])


# ---- aggregate_dimensions ----


def test_aggregate_dimensions_counts_scenarios_once_per_value():
    details = [
        {
            "scenarios": [
                {
                    "combos": [
                        {"release": "jammy", "machine_type": "lxd-container"},
                        {"release": "jammy", "machine_type": "lxd-vm"},
                    ]
                },
                {
                    "combos": [
                        {"release": "resolute", "machine_type": "lxd-vm"}
                    ]
                },
            ]
        }
    ]
    dimensions = domain.aggregate_dimensions(details)
    assert dimensions["releases"] == [
        {"name": "jammy", "scenario_count": 1},
        {"name": "resolute", "scenario_count": 1},
    ]
    assert dimensions["machine_types"] == [
        {"name": "lxd-container", "scenario_count": 1},
        {"name": "lxd-vm", "scenario_count": 2},
    ]
