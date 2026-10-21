import pytest

from behave_campaign.domain.discovery import (
    available_dimensions,
    discover_units,
)
from behave_campaign.domain.records import CampaignError, Scope, Unit

FEATURE = """@uses.config.contract_token
Feature: Example feature

  Scenario Outline: Runs everywhere
    Given a `<release>` `<machine_type>` machine
    Then I verify that `esm-infra` is enabled

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
      | noble   | lxd-vm        |

  Scenario Outline: Runs on containers only
    Given a `<release>` `<machine_type>` machine
    Then I verify that `esm-apps` is enabled

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
"""


# Two outlines with one name. The second's row decides whether they collide.
SHARED_NAME_FEATURE = """Feature: Shared names

  Scenario Outline: Shared
    Given a `<release>` `<machine_type>` machine

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |

  Scenario Outline: Shared
    Given a `<release>` `<machine_type>` machine

    Examples: ubuntu release
      | release | machine_type  |
      | {second_row} |
"""


def write_feature(repo_root, text):
    features = repo_root / "features"
    features.mkdir()
    (features / "shared.feature").write_text(text)


@pytest.fixture
def repo(tmp_path):
    features = tmp_path / "features"
    features.mkdir()
    (features / "example.feature").write_text(FEATURE)
    return tmp_path


class TestDiscoverUnits:
    def test_every_combo_becomes_a_unit(self, repo):
        units = discover_units(repo, Scope())

        assert units == [
            Unit(
                "features/example.feature",
                "Runs everywhere",
                "jammy",
                "lxd-container",
            ),
            Unit(
                "features/example.feature",
                "Runs everywhere",
                "noble",
                "lxd-vm",
            ),
            Unit(
                "features/example.feature",
                "Runs on containers only",
                "jammy",
                "lxd-container",
            ),
        ]

    def test_release_filter_selects_a_campaign_slice(self, repo):
        units = discover_units(repo, Scope(release=("noble",)))

        assert [unit.scenario for unit in units] == ["Runs everywhere"]
        assert units[0].machine_type == "lxd-vm"

    def test_machine_type_filter_selects_a_campaign_slice(self, repo):
        units = discover_units(repo, Scope(machine_type=("lxd-container",)))

        assert {unit.release for unit in units} == {"jammy"}
        assert len(units) == 2

    def test_filters_combine(self, repo):
        units = discover_units(
            repo,
            Scope(
                release=("jammy",),
                scenario=("Runs on containers only",),
            ),
        )

        assert len(units) == 1
        assert units[0].scenario == "Runs on containers only"

    def test_unknown_release_is_rejected(self, repo):
        with pytest.raises(CampaignError, match="unknown release: bogus"):
            discover_units(repo, Scope(release=("bogus",)))

    def test_unknown_machine_type_is_rejected(self, repo):
        with pytest.raises(CampaignError, match="unknown machine_type"):
            discover_units(repo, Scope(machine_type=("lxd-toaster",)))

    def test_unknown_feature_is_rejected(self, repo):
        with pytest.raises(CampaignError, match="unknown feature"):
            discover_units(repo, Scope(feature=("features/nope.feature",)))

    def test_mistyped_feature_suggests_close_matches(self, tmp_path):
        features = tmp_path / "features"
        features.mkdir()
        for index in range(20):
            (features / "example{}.feature".format(index)).write_text(
                FEATURE.replace("Runs everywhere", "Runs {}".format(index))
            )

        with pytest.raises(CampaignError) as error:
            discover_units(
                tmp_path, Scope(feature=("features/example7.featur",))
            )

        message = str(error.value)
        assert "did you mean: features/example7.feature" in message
        assert "features/example1.feature" not in message

    def test_a_shared_name_on_a_shared_combo_is_refused(self, tmp_path):
        # Both outlines run "Shared" on jammy lxd-container, so behave would
        # run them both for that unit.
        write_feature(
            tmp_path,
            SHARED_NAME_FEATURE.format(second_row="jammy   | lxd-container"),
        )

        with pytest.raises(CampaignError) as error:
            discover_units(tmp_path, Scope())

        assert "1 unit(s) in scope are run by more than one scenario" in str(
            error.value
        )
        assert "Shared jammy lxd-container" in str(error.value)

    def test_a_shared_name_on_other_combos_is_two_units(self, tmp_path):
        write_feature(
            tmp_path,
            SHARED_NAME_FEATURE.format(second_row="noble   | lxd-vm       "),
        )

        units = discover_units(tmp_path, Scope())

        assert [(u.release, u.machine_type) for u in units] == [
            ("jammy", "lxd-container"),
            ("noble", "lxd-vm"),
        ]

    def test_a_scope_that_leaves_out_the_collision_is_allowed(self, tmp_path):
        write_feature(
            tmp_path,
            SHARED_NAME_FEATURE.format(second_row="jammy   | lxd-container"),
        )
        (tmp_path / "features" / "example.feature").write_text(FEATURE)

        units = discover_units(
            tmp_path, Scope(feature=("features/example.feature",))
        )

        assert {u.feature for u in units} == {"features/example.feature"}

    def test_repo_without_features_is_rejected(self, tmp_path):
        with pytest.raises(CampaignError, match="no feature files found"):
            discover_units(tmp_path, Scope())


class TestAvailableDimensions:
    def test_reports_releases_and_machine_types_with_counts(self, repo):
        dimensions = available_dimensions(repo)

        assert dimensions["releases"] == [
            {"name": "jammy", "scenario_count": 2},
            {"name": "noble", "scenario_count": 1},
        ]
        assert [value["name"] for value in dimensions["machine_types"]] == [
            "lxd-container",
            "lxd-vm",
        ]
