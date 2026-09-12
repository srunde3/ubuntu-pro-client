import pytest

from campaign.discovery import available_dimensions, discover_units
from campaign.domain import CampaignError, Filters, Unit

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


@pytest.fixture
def repo(tmp_path):
    features = tmp_path / "features"
    features.mkdir()
    (features / "example.feature").write_text(FEATURE)
    return tmp_path


class TestDiscoverUnits:
    def test_every_combo_becomes_a_unit(self, repo):
        units = discover_units(repo, Filters())

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
        units = discover_units(repo, Filters(release=("noble",)))

        assert [unit.scenario for unit in units] == ["Runs everywhere"]
        assert units[0].machine_type == "lxd-vm"

    def test_machine_type_filter_selects_a_campaign_slice(self, repo):
        units = discover_units(repo, Filters(machine_type=("lxd-container",)))

        assert {unit.release for unit in units} == {"jammy"}
        assert len(units) == 2

    def test_filters_combine(self, repo):
        units = discover_units(
            repo,
            Filters(
                release=("jammy",),
                scenario=("Runs on containers only",),
            ),
        )

        assert len(units) == 1
        assert units[0].scenario == "Runs on containers only"

    def test_unknown_release_is_rejected(self, repo):
        with pytest.raises(CampaignError, match="unknown release: bogus"):
            discover_units(repo, Filters(release=("bogus",)))

    def test_unknown_machine_type_is_rejected(self, repo):
        with pytest.raises(CampaignError, match="unknown machine_type"):
            discover_units(repo, Filters(machine_type=("lxd-toaster",)))

    def test_unknown_feature_is_rejected(self, repo):
        with pytest.raises(CampaignError, match="unknown feature"):
            discover_units(repo, Filters(feature=("features/nope.feature",)))

    def test_mistyped_feature_suggests_close_matches(self, tmp_path):
        features = tmp_path / "features"
        features.mkdir()
        for index in range(20):
            (features / "example{}.feature".format(index)).write_text(
                FEATURE.replace("Runs everywhere", "Runs {}".format(index))
            )

        with pytest.raises(CampaignError) as error:
            discover_units(
                tmp_path, Filters(feature=("features/example7.featur",))
            )

        message = str(error.value)
        assert "did you mean: features/example7.feature" in message
        assert "features/example1.feature" not in message

    def test_repo_without_features_is_rejected(self, tmp_path):
        with pytest.raises(CampaignError, match="no feature files found"):
            discover_units(tmp_path, Filters())


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
