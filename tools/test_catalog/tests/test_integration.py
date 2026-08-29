"""Integration tests: parsing + builder + schema + db wired together via
the library API (TestCatalog), against the small synthetic fixtures in
conftest.py -- not through the CLI. See test_integration_real_corpus.py
for the same layer against the real features/ corpus."""

import os
import sqlite3
import time
from pathlib import Path

import pytest

from behave_catalog.db import Combo, TestCatalog
from behave_catalog.schema import SCHEMA_VERSION


@pytest.fixture
def catalog(repo_root: Path, tmp_path: Path) -> TestCatalog:
    db_path = tmp_path / "catalog.sqlite3"
    built = TestCatalog.build(repo_root, db_path)
    yield built
    built.close()


def test_dimensions(catalog: TestCatalog) -> None:
    dims = catalog.dimensions()
    releases = {value.name: value.scenario_count for value in dims.releases}
    machine_types = {
        value.name: value.scenario_count for value in dims.machine_types
    }
    assert releases == {"jammy": 1, "focal": 2}
    assert machine_types == {"lxd-container": 1, "lxd-vm": 2}


def test_features_unfiltered_lists_both(catalog: TestCatalog) -> None:
    entries = catalog.features()
    assert {entry.path for entry in entries} == {
        "features/apt.feature",
        "features/fips.feature",
    }
    apt_entry = next(
        entry for entry in entries if entry.path == "features/apt.feature"
    )
    assert apt_entry.scenario_count == 1
    assert apt_entry.requires_config == ("contract_token",)
    assert apt_entry.releases == ("focal", "jammy")
    assert apt_entry.machine_types == ("lxd-container", "lxd-vm")


def test_features_filtered_by_tag(catalog: TestCatalog) -> None:
    entries = catalog.features(tag="security")
    assert [entry.path for entry in entries] == ["features/apt.feature"]

    assert catalog.features(tag="does-not-exist") == []


def test_query_requires_same_combo_row_for_release_and_machine_type(
    catalog: TestCatalog,
) -> None:
    """A scenario touching focal (via one combo) and lxd-vm (via a
    different combo) must not match a query for focal+lxd-vm together --
    only a scenario with a single combo satisfying both should."""
    matches = catalog.query(releases=["focal"], machine_types=["lxd-vm"])
    assert [match.name for match in matches] == [
        "Enable FIPS on a fixed machine"
    ]
    assert matches[0].combos == (Combo("focal", "lxd-vm"),)

    # Sanity check: focal alone (independent of machine_type) matches both.
    focal_only = catalog.query(releases=["focal"])
    assert {match.name for match in focal_only} == {
        "Enable FIPS on a fixed machine",
        "Install a package on <release> <machine_type>",
    }


def test_query_tags_any_and_tags_all(catalog: TestCatalog) -> None:
    any_matches = catalog.query(tags_any=["security"])
    assert len(any_matches) == 1

    all_matches = catalog.query(tags_all=["security", "slow"])
    assert len(all_matches) == 1

    assert catalog.query(tags_all=["security", "does-not-exist"]) == []


def test_search_full_text(catalog: TestCatalog) -> None:
    apt_matches = catalog.search("apt")
    assert [match.name for match in apt_matches] == [
        "Install a package on <release> <machine_type>"
    ]

    fips_matches = catalog.search("fips")
    assert [match.name for match in fips_matches] == [
        "Enable FIPS on a fixed machine"
    ]

    # Punctuation/hyphens in free text must not blow up the FTS5 query.
    assert catalog.search("apt-get; DROP TABLE scenarios --") == []


def test_open_or_build_rebuilds_on_source_change(
    repo_root: Path, tmp_path: Path
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    with TestCatalog.build(repo_root, db_path):
        pass

    fips_path = repo_root / "features" / "fips.feature"
    fips_path.write_text(
        fips_path.read_text()
        + "\n  Scenario: A brand new scenario\n"
        + "    Given a `jammy` `lxd-container` machine with "
        + "ubuntu-advantage-tools installed\n"
    )
    # Force a distinct mtime in case the filesystem's clock resolution is
    # coarser than the time this test takes to run.
    future = time.time() + 5
    os.utime(fips_path, (future, future))

    with TestCatalog.open_or_build(repo_root, db_path) as reopened:
        fips_entry = next(
            entry
            for entry in reopened.features()
            if entry.path == "features/fips.feature"
        )
        assert fips_entry.scenario_count == 2


def test_open_or_build_reuses_fresh_db(
    repo_root: Path, tmp_path: Path
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    with TestCatalog.build(repo_root, db_path):
        pass
    built_mtime = db_path.stat().st_mtime_ns

    with TestCatalog.open_or_build(repo_root, db_path):
        pass

    assert db_path.stat().st_mtime_ns == built_mtime


def test_open_or_build_rebuilds_on_schema_version_mismatch(
    repo_root: Path, tmp_path: Path
) -> None:
    """Even with source files untouched, a stale schema_version alone must
    force a rebuild -- otherwise a DB built under an old schema looks
    "fresh" forever and the next query breaks against the new DDL."""
    db_path = tmp_path / "catalog.sqlite3"
    with TestCatalog.build(repo_root, db_path):
        pass
    built_mtime = db_path.stat().st_mtime_ns

    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION - 1),),
    )
    connection.commit()
    connection.close()

    with TestCatalog.open_or_build(repo_root, db_path) as reopened:
        assert reopened.dimensions() is not None
    assert db_path.stat().st_mtime_ns != built_mtime


def test_is_fresh_returns_false_for_a_non_database_file(
    repo_root: Path, tmp_path: Path
) -> None:
    corrupted = tmp_path / "corrupted.sqlite3"
    corrupted.write_text("this is not a sqlite database")
    assert TestCatalog._is_fresh(repo_root, corrupted) is False


def test_is_fresh_returns_false_for_a_directory(
    repo_root: Path, tmp_path: Path
) -> None:
    a_directory = tmp_path / "a_directory"
    a_directory.mkdir()
    assert TestCatalog._is_fresh(repo_root, a_directory) is False


def test_is_fresh_returns_false_when_fingerprint_row_is_missing(
    repo_root: Path, tmp_path: Path
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    with TestCatalog.build(repo_root, db_path):
        pass

    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM meta WHERE key = 'source_fingerprint'")
    connection.commit()
    connection.close()

    assert TestCatalog._is_fresh(repo_root, db_path) is False


def test_query_empty_list_filter_means_no_matches(
    catalog: TestCatalog,
) -> None:
    """An explicit empty list ("filtered down to nothing") must return no
    matches -- not be silently treated the same as None ("unfiltered")."""
    assert catalog.query(releases=[]) == []
    assert catalog.query(machine_types=[]) == []
    assert catalog.query(tags_any=[]) == []
    assert catalog.query(tags_all=[]) == []
    # Mixing an empty list with an otherwise-valid filter still matches
    # nothing -- the impossible filter dominates.
    assert catalog.query(releases=["focal"], machine_types=[]) == []

    # Sanity check: omitting a filter (None) is genuinely unfiltered.
    assert catalog.query(releases=None) != []


def test_search_orders_by_relevance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    features_dir = repo / "features"
    features_dir.mkdir(parents=True)
    (features_dir / "strong.feature").write_text(
        "Feature: apt apt apt\n"
        "\n"
        "  Scenario: apt apt apt\n"
        "    Given a `jammy` `lxd-container` machine with "
        "ubuntu-advantage-tools installed\n"
        "    When I run `apt` with sudo\n"
    )
    (features_dir / "weak.feature").write_text(
        "Feature: unrelated behavior\n"
        "\n"
        "  Scenario: mentions apt only once, in passing\n"
        "    Given a `jammy` `lxd-container` machine with "
        "ubuntu-advantage-tools installed\n"
        "    When I run `apt` with sudo\n"
        "    Then something completely unrelated happens\n"
    )

    with TestCatalog.build(repo, tmp_path / "catalog.sqlite3") as ranked:
        matches = ranked.search("apt")

    assert [match.feature_path for match in matches] == [
        "features/strong.feature",
        "features/weak.feature",
    ]
