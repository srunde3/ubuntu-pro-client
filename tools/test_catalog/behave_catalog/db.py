"""Read-optimized query API over a built test catalog database."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from behave_catalog.builder import build_database
from behave_catalog.discovery import discover_feature_files, source_fingerprint
from behave_catalog.parsing import Combo
from behave_catalog.schema import SCHEMA_VERSION


@dataclass(frozen=True)
class FeatureEntry:
    """Lightweight catalog projection of a feature."""

    path: str
    title: str
    scenario_count: int
    requires_config: tuple[str, ...]
    releases: tuple[str, ...]
    machine_types: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioMatch:
    """A scenario hit, with the combos satisfying the query's filters."""

    feature_path: str
    name: str
    type: str
    tags: tuple[str, ...]
    requires_config: tuple[str, ...]
    combos: tuple[Combo, ...]


@dataclass(frozen=True)
class DimensionValue:
    """A distinct release or machine_type with its scenario count."""

    name: str
    scenario_count: int


@dataclass(frozen=True)
class Dimensions:
    """Distinct releases and machine_types across the suite."""

    releases: tuple[DimensionValue, ...]
    machine_types: tuple[DimensionValue, ...]


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression (AND of terms).

    Quoting each token as an FTS5 string literal keeps arbitrary caller
    text (hyphens, quotes, punctuation) from being interpreted as FTS5
    query syntax.
    """
    tokens = text.split()
    if not tokens:
        return '""'
    escaped = ['"' + token.replace('"', '""') + '"' for token in tokens]
    return " AND ".join(escaped)


class TestCatalog:
    """Read-optimized, SQLite-backed view over parsed behave feature files.

    Construct via ``build``, ``open``, or ``open_or_build`` -- not directly.
    """

    # Not a pytest test class -- pytest's "Test*" collection heuristic
    # would otherwise try to instantiate this as one.
    __test__ = False

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # -- construction ------------------------------------------------------
    @classmethod
    def build(cls, repo_root: Path, db_path: Path) -> "TestCatalog":
        build_database(repo_root, db_path)
        return cls.open(db_path)

    @classmethod
    def open(cls, db_path: Path) -> "TestCatalog":
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return cls(connection)

    @classmethod
    def open_or_build(cls, repo_root: Path, db_path: Path) -> "TestCatalog":
        """Open ``db_path``, rebuilding first if it's missing or stale.

        If two processes call this concurrently against a stale db_path,
        both may rebuild; whichever's atomic rename lands last wins, with
        no guarantee it's the one holding the newest fingerprint. Harmless
        for the expected usage (one CLI invocation or one server process
        at a time) -- a fleet of concurrent builders would need real
        locking, which this doesn't attempt.
        """
        if db_path.exists() and cls._is_fresh(repo_root, db_path):
            return cls.open(db_path)
        build_database(repo_root, db_path)
        return cls.open(db_path)

    @staticmethod
    def _is_fresh(repo_root: Path, db_path: Path) -> bool:
        # sqlite3.DatabaseError is the parent of OperationalError -- catch
        # it, not just OperationalError, since a non-sqlite/corrupted
        # db_path raises DatabaseError directly ("file is not a database")
        # rather than the narrower OperationalError a locked/inaccessible
        # database raises.
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return False
        try:
            rows = dict(connection.execute("SELECT key, value FROM meta"))
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()

        # A schema change alone (no .feature files touched) must still
        # force a rebuild -- otherwise a DB built under an older schema
        # looks "fresh" forever and the next query breaks or misbehaves
        # against the new DDL.
        if rows.get("schema_version") != str(SCHEMA_VERSION):
            return False

        fingerprint = rows.get("source_fingerprint")
        if fingerprint is None:
            return False
        feature_files = discover_feature_files(repo_root)
        return fingerprint == source_fingerprint(repo_root, feature_files)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "TestCatalog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- queries -------------------------------------------------------
    def dimensions(self) -> Dimensions:
        releases = self._connection.execute(
            "SELECT release AS name, COUNT(DISTINCT scenario_id) AS n "
            "FROM combos GROUP BY release ORDER BY release"
        ).fetchall()
        machine_types = self._connection.execute(
            "SELECT machine_type AS name, COUNT(DISTINCT scenario_id) AS n "
            "FROM combos GROUP BY machine_type ORDER BY machine_type"
        ).fetchall()
        return Dimensions(
            releases=tuple(
                DimensionValue(row["name"], row["n"]) for row in releases
            ),
            machine_types=tuple(
                DimensionValue(row["name"], row["n"]) for row in machine_types
            ),
        )

    def features(
        self,
        *,
        release: Optional[str] = None,
        machine_type: Optional[str] = None,
        tag: Optional[str] = None,
        text: Optional[str] = None,
    ) -> list[FeatureEntry]:
        """Feature-level catalog entries with at least one matching scenario.

        With no filters, every feature is returned.
        """
        if (
            release is None
            and machine_type is None
            and tag is None
            and text is None
        ):
            paths = [
                row["path"]
                for row in self._connection.execute(
                    "SELECT path FROM features ORDER BY path"
                ).fetchall()
            ]
            return [self._feature_entry(path) for path in paths]

        scenario_ids = self._scenario_ids(
            releases=[release] if release else None,
            machine_types=[machine_type] if machine_type else None,
            tags_any=[tag] if tag else None,
            text=text,
        )
        if not scenario_ids:
            return []
        placeholders = ",".join("?" for _ in scenario_ids)
        rows = self._connection.execute(
            "SELECT DISTINCT feature_path FROM scenarios "
            f"WHERE id IN ({placeholders}) ORDER BY feature_path",
            scenario_ids,
        ).fetchall()
        return [self._feature_entry(row["feature_path"]) for row in rows]

    def _feature_entry(self, path: str) -> FeatureEntry:
        feature_row = self._connection.execute(
            "SELECT title, requires_config FROM features WHERE path = ?",
            (path,),
        ).fetchone()
        scenario_rows = self._connection.execute(
            "SELECT id, requires_config FROM scenarios "
            "WHERE feature_path = ?",
            (path,),
        ).fetchall()
        scenario_ids = [row["id"] for row in scenario_rows]

        requires_config: set[str] = set(
            json.loads(feature_row["requires_config"])
        )
        for row in scenario_rows:
            requires_config.update(json.loads(row["requires_config"]))

        releases: set[str] = set()
        machine_types: set[str] = set()
        if scenario_ids:
            placeholders = ",".join("?" for _ in scenario_ids)
            combo_rows = self._connection.execute(
                "SELECT DISTINCT release, machine_type FROM combos "
                f"WHERE scenario_id IN ({placeholders})",
                scenario_ids,
            ).fetchall()
            for row in combo_rows:
                releases.add(row["release"])
                machine_types.add(row["machine_type"])

        return FeatureEntry(
            path=path,
            title=feature_row["title"],
            scenario_count=len(scenario_ids),
            requires_config=tuple(sorted(requires_config)),
            releases=tuple(sorted(releases)),
            machine_types=tuple(sorted(machine_types)),
        )

    def find_scenarios(
        self,
        *,
        release: Optional[str] = None,
        machine_type: Optional[str] = None,
        tag: Optional[str] = None,
        text: Optional[str] = None,
    ) -> list[ScenarioMatch]:
        """Point-query convenience over ``query`` (MCP's filter shape)."""
        return self.query(
            releases=[release] if release else None,
            machine_types=[machine_type] if machine_type else None,
            tags_any=[tag] if tag else None,
            text=text,
        )

    def query(
        self,
        *,
        releases: Optional[Sequence[str]] = None,
        machine_types: Optional[Sequence[str]] = None,
        tags_any: Optional[Sequence[str]] = None,
        tags_all: Optional[Sequence[str]] = None,
        text: Optional[str] = None,
        limit: int = 200,
    ) -> list[ScenarioMatch]:
        """Composable scenario search: every filter given is AND-ed together.

        ``releases``/``machine_types`` require a *single* combo row to
        satisfy both (a scenario covering jammy+lxd-vm and, separately,
        focal+lxd-container does not match releases=[jammy],
        machine_types=[lxd-container]) -- this mirrors what
        ``-D releases=... -D machine_types=...`` actually runs.
        """
        scenario_ids = self._scenario_ids(
            releases=releases,
            machine_types=machine_types,
            tags_any=tags_any,
            tags_all=tags_all,
            text=text,
        )
        scenario_ids = scenario_ids[:limit]
        return [
            self._scenario_match(scenario_id, releases, machine_types)
            for scenario_id in scenario_ids
        ]

    def search(self, text: str, *, limit: int = 50) -> list[ScenarioMatch]:
        """Full-text keyword search over scenario name/tags/step bodies."""
        return self.query(text=text, limit=limit)

    def _scenario_ids(
        self,
        *,
        releases: Optional[Sequence[str]] = None,
        machine_types: Optional[Sequence[str]] = None,
        tags_any: Optional[Sequence[str]] = None,
        tags_all: Optional[Sequence[str]] = None,
        text: Optional[str] = None,
    ) -> list[int]:
        # An explicit empty list is a filter that can match nothing --
        # distinct from None ("no filter on this dimension"). Without this,
        # `releases or machine_types`-style truthy checks below would treat
        # an empty list the same as unfiltered, silently widening a
        # caller's empty candidate set (e.g. a list of releases computed
        # upstream that happened to come back empty) into "match
        # everything".
        if any(
            value is not None and len(value) == 0
            for value in (releases, machine_types, tags_any, tags_all)
        ):
            return []

        candidate: Optional[set[int]] = None
        ranked_order: Optional[list[int]] = None

        def _intersect(ids: set[int]) -> None:
            nonlocal candidate
            candidate = ids if candidate is None else candidate & ids

        if releases or machine_types:
            clauses = []
            params: list[str] = []
            if releases:
                placeholders = ",".join("?" for _ in releases)
                clauses.append(f"release IN ({placeholders})")
                params.extend(releases)
            if machine_types:
                placeholders = ",".join("?" for _ in machine_types)
                clauses.append(f"machine_type IN ({placeholders})")
                params.extend(machine_types)
            rows = self._connection.execute(
                "SELECT DISTINCT scenario_id FROM combos WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchall()
            _intersect({row["scenario_id"] for row in rows})

        if tags_any:
            placeholders = ",".join("?" for _ in tags_any)
            rows = self._connection.execute(
                "SELECT DISTINCT scenario_id FROM tags "
                f"WHERE tag IN ({placeholders})",
                list(tags_any),
            ).fetchall()
            _intersect({row["scenario_id"] for row in rows})

        if tags_all:
            placeholders = ",".join("?" for _ in tags_all)
            rows = self._connection.execute(
                "SELECT scenario_id FROM tags "
                f"WHERE tag IN ({placeholders}) "
                "GROUP BY scenario_id HAVING COUNT(DISTINCT tag) = ?",
                [*tags_all, len(tags_all)],
            ).fetchall()
            _intersect({row["scenario_id"] for row in rows})

        if text:
            rows = self._connection.execute(
                "SELECT scenario_id FROM scenarios_fts "
                "WHERE scenarios_fts MATCH ? "
                "ORDER BY bm25(scenarios_fts)",
                (_fts_query(text),),
            ).fetchall()
            ranked_order = [row["scenario_id"] for row in rows]
            _intersect(set(ranked_order))

        if candidate is None:
            rows = self._connection.execute(
                "SELECT id FROM scenarios ORDER BY id"
            ).fetchall()
            return [row["id"] for row in rows]

        if ranked_order is not None:
            # Preserve FTS relevance order (best match first), narrowed to
            # whatever other AND-ed filters also matched -- a plain
            # `sorted(candidate)` here would discard bm25 ranking entirely
            # and return matches in arbitrary rowid order instead.
            return [
                scenario_id
                for scenario_id in ranked_order
                if scenario_id in candidate
            ]
        return sorted(candidate)

    def _scenario_match(
        self,
        scenario_id: int,
        releases: Optional[Sequence[str]],
        machine_types: Optional[Sequence[str]],
    ) -> ScenarioMatch:
        scenario_row = self._connection.execute(
            "SELECT feature_path, name, type, tags, requires_config "
            "FROM scenarios WHERE id = ?",
            (scenario_id,),
        ).fetchone()
        combo_rows = self._connection.execute(
            "SELECT release, machine_type FROM combos WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchall()
        combos = [
            Combo(row["release"], row["machine_type"]) for row in combo_rows
        ]
        if releases:
            combos = [combo for combo in combos if combo.release in releases]
        if machine_types:
            combos = [
                combo
                for combo in combos
                if combo.machine_type in machine_types
            ]

        return ScenarioMatch(
            feature_path=scenario_row["feature_path"],
            name=scenario_row["name"],
            type=scenario_row["type"],
            tags=tuple(json.loads(scenario_row["tags"])),
            requires_config=tuple(json.loads(scenario_row["requires_config"])),
            combos=tuple(combos),
        )
