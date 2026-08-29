"""End-to-end tests: through the actual CLI entrypoint (cli.main), the
same way a real invocation of the `test-catalog` command would run."""

import json
import os
import time
from pathlib import Path

import pytest

from behave_catalog import cli


def test_build_creates_db(repo_root: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    exit_code = cli.main(
        ["build", "--repo-root", str(repo_root), "--db", str(db_path)]
    )
    assert exit_code == 0
    assert db_path.exists()


def test_query_json_output(
    repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    cli.main(["build", "--repo-root", str(repo_root), "--db", str(db_path)])
    capsys.readouterr()

    exit_code = cli.main(
        [
            "query",
            "--repo-root",
            str(repo_root),
            "--db",
            str(db_path),
            "--release",
            "focal",
            "--machine-type",
            "lxd-vm",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [match["name"] for match in payload] == [
        "Enable FIPS on a fixed machine"
    ]


def test_search_finds_expected_scenario(
    repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    cli.main(["build", "--repo-root", str(repo_root), "--db", str(db_path)])
    capsys.readouterr()

    cli.main(
        ["search", "apt", "--repo-root", str(repo_root), "--db", str(db_path)]
    )
    output = capsys.readouterr().out
    assert "Install a package on" in output


def test_dimensions_table_output(
    repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    cli.main(["build", "--repo-root", str(repo_root), "--db", str(db_path)])
    capsys.readouterr()

    cli.main(
        [
            "dimensions",
            "--repo-root",
            str(repo_root),
            "--db",
            str(db_path),
        ]
    )
    output = capsys.readouterr().out
    assert "releases:" in output
    assert "machine_types:" in output


def test_features_json_output(
    repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    cli.main(["build", "--repo-root", str(repo_root), "--db", str(db_path)])
    capsys.readouterr()

    exit_code = cli.main(
        [
            "features",
            "--repo-root",
            str(repo_root),
            "--db",
            str(db_path),
            "--tag",
            "security",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["path"] for entry in payload] == ["features/apt.feature"]


def test_editing_a_feature_file_produces_a_new_db_via_the_cli(
    repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The auto-rebuild-on-staleness behavior isn't just a library-level
    guarantee (see test_integration.py) -- confirm it end-to-end: build via
    the CLI, edit a .feature file, and see the new scenario appear on the
    next CLI command with no explicit rebuild in between."""
    db_path = tmp_path / "catalog.sqlite3"
    cli.main(["build", "--repo-root", str(repo_root), "--db", str(db_path)])
    capsys.readouterr()
    built_mtime = db_path.stat().st_mtime_ns

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

    exit_code = cli.main(
        [
            "features",
            "--repo-root",
            str(repo_root),
            "--db",
            str(db_path),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    assert db_path.stat().st_mtime_ns != built_mtime

    payload = json.loads(capsys.readouterr().out)
    fips_entry = next(
        entry for entry in payload if entry["path"] == "features/fips.feature"
    )
    assert fips_entry["scenario_count"] == 2


def test_nonexistent_repo_root_exits_cleanly_without_touching_disk(
    tmp_path: Path,
) -> None:
    bogus_root = tmp_path / "does" / "not" / "exist"
    with pytest.raises(SystemExit):
        cli.main(["build", "--repo-root", str(bogus_root)])
    # The whole point of the guard: no directories get created en route to
    # a --db path derived from an invalid --repo-root.
    assert not bogus_root.exists()


def test_repo_root_missing_features_dir_exits_cleanly(tmp_path: Path) -> None:
    not_a_checkout = tmp_path / "some_other_project"
    not_a_checkout.mkdir()
    with pytest.raises(SystemExit):
        cli.main(["features", "--repo-root", str(not_a_checkout)])


def test_os_error_during_a_command_exits_cleanly_instead_of_a_traceback(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers narrow filesystem races (e.g. a .feature file deleted between
    discovery and fingerprinting) surfacing as OSError mid-command -- the
    CLI should fail with a clean message, not propagate a raw traceback."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr("behave_catalog.builder.discover_feature_files", _boom)

    db_path = tmp_path / "catalog.sqlite3"
    with pytest.raises(SystemExit, match="test-catalog: boom"):
        cli.main(
            ["build", "--repo-root", str(repo_root), "--db", str(db_path)]
        )


def test_auto_detect_repo_root(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    monkeypatch.chdir(repo_root / "features")
    exit_code = cli.main(["build", "--db", str(db_path)])
    assert exit_code == 0
    assert db_path.exists()
