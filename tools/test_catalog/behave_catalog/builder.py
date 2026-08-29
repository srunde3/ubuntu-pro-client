"""Builds a SQLite-backed test catalog from behave feature files."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Sequence

from behave_catalog.discovery import discover_feature_files, source_fingerprint
from behave_catalog.parsing import FeatureRecord, parse_feature_file
from behave_catalog.schema import DDL_STATEMENTS, SCHEMA_VERSION


def build_database(repo_root: Path, db_path: Path) -> None:
    """Parse every feature file under ``repo_root`` into a fresh catalog DB.

    Writes to a temporary file in ``db_path``'s directory and renames it
    into place, so a reader opening ``db_path`` mid-build never sees a
    partially populated database.
    """
    feature_files = discover_feature_files(repo_root)
    fingerprint = source_fingerprint(repo_root, feature_files)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=str(db_path.parent), prefix=".test_catalog-", suffix=".sqlite3"
    )
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    # sqlite3.connect must create the file itself.
    tmp_path.unlink()

    try:
        connection = sqlite3.connect(tmp_path)
        try:
            _write_database(connection, repo_root, feature_files, fingerprint)
        finally:
            connection.close()
        tmp_path.replace(db_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_database(
    connection: sqlite3.Connection,
    repo_root: Path,
    feature_files: Sequence[str],
    fingerprint: str,
) -> None:
    connection.executescript(DDL_STATEMENTS)
    connection.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    connection.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        ("source_fingerprint", fingerprint),
    )

    for relative_path in feature_files:
        feature = parse_feature_file(repo_root / relative_path, relative_path)
        if feature is not None:
            _insert_feature(connection, feature)

    connection.commit()


def _insert_feature(
    connection: sqlite3.Connection, feature: FeatureRecord
) -> None:
    connection.execute(
        "INSERT INTO features(path, title, tags, requires_config) "
        "VALUES (?, ?, ?, ?)",
        (
            feature.path,
            feature.title,
            json.dumps(list(feature.tags)),
            json.dumps(list(feature.requires_config)),
        ),
    )
    for scenario in feature.scenarios:
        cursor = connection.execute(
            "INSERT INTO scenarios("
            "feature_path, name, type, tags, requires_config, "
            "example_columns) VALUES (?, ?, ?, ?, ?, ?)",
            (
                feature.path,
                scenario.name,
                scenario.type,
                json.dumps(list(scenario.tags)),
                json.dumps(list(scenario.requires_config)),
                json.dumps(list(scenario.example_columns)),
            ),
        )
        scenario_id = cursor.lastrowid

        if scenario.combos:
            connection.executemany(
                "INSERT INTO combos(scenario_id, release, machine_type) "
                "VALUES (?, ?, ?)",
                [
                    (scenario_id, combo.release, combo.machine_type)
                    for combo in scenario.combos
                ],
            )

        tag_rows = [(scenario_id, tag, "feature") for tag in feature.tags]
        tag_rows += [(scenario_id, tag, "scenario") for tag in scenario.tags]
        if tag_rows:
            connection.executemany(
                "INSERT INTO tags(scenario_id, tag, source) VALUES (?, ?, ?)",
                tag_rows,
            )

        connection.execute(
            "INSERT INTO scenarios_fts(scenario_id, document) VALUES (?, ?)",
            (scenario_id, scenario.document),
        )
