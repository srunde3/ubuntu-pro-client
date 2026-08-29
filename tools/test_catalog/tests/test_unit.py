"""Unit tests: one function (parse_feature_file), no I/O beyond a single
fixture file, no database."""

from pathlib import Path

from behave_catalog.parsing import Combo, parse_feature_file


def test_apt_feature_combos_from_examples_table(repo_root: Path) -> None:
    feature = parse_feature_file(
        repo_root / "features" / "apt.feature", "features/apt.feature"
    )
    assert feature is not None
    assert feature.title == "apt behavior"
    assert feature.requires_config == ("contract_token",)
    assert len(feature.scenarios) == 1

    scenario = feature.scenarios[0]
    assert scenario.type == "scenario_outline"
    assert set(scenario.tags) == {"security", "slow"}
    assert scenario.requires_config == ("contract_token",)
    assert scenario.example_columns == ("release", "machine_type")
    assert set(scenario.combos) == {
        Combo("jammy", "lxd-container"),
        Combo("jammy", "lxd-vm"),
        Combo("focal", "lxd-container"),
    }
    assert "apt install curl" in scenario.document


def test_fips_feature_combo_from_step_override(repo_root: Path) -> None:
    feature = parse_feature_file(
        repo_root / "features" / "fips.feature", "features/fips.feature"
    )
    assert feature is not None
    assert feature.requires_config == ()
    scenario = feature.scenarios[0]
    assert scenario.tags == ()
    assert scenario.combos == (Combo("focal", "lxd-vm"),)
    assert "fips" in scenario.document.lower()


def test_unparsable_file_returns_none(tmp_path: Path) -> None:
    bad_file = tmp_path / "broken.feature"
    bad_file.write_text("not a real feature file at all {{{")
    assert parse_feature_file(bad_file, "broken.feature") is None
