"""SQLite schema for the test catalog database."""

SCHEMA_VERSION = 1

DDL_STATEMENTS = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE features (
    path TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    tags TEXT NOT NULL,             -- JSON array
    requires_config TEXT NOT NULL   -- JSON array
);

CREATE TABLE scenarios (
    id INTEGER PRIMARY KEY,
    feature_path TEXT NOT NULL REFERENCES features(path),
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    tags TEXT NOT NULL,              -- JSON array, scenario's own tags
    requires_config TEXT NOT NULL,   -- JSON array, effective tags' union
    example_columns TEXT NOT NULL    -- JSON array
);
CREATE INDEX idx_scenarios_feature ON scenarios(feature_path);

CREATE TABLE combos (
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id),
    release TEXT NOT NULL,
    machine_type TEXT NOT NULL
);
CREATE INDEX idx_combos_release ON combos(release);
CREATE INDEX idx_combos_machine_type ON combos(machine_type);
CREATE INDEX idx_combos_scenario ON combos(scenario_id);

-- feature-tags and scenario-tags are both loaded here (with `source`
-- recording which) so tag search covers effective tags without a join
-- back to `features` on every query.
CREATE TABLE tags (
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id),
    tag TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX idx_tags_tag ON tags(tag);
CREATE INDEX idx_tags_scenario ON tags(scenario_id);

CREATE VIRTUAL TABLE scenarios_fts USING fts5(
    scenario_id UNINDEXED,
    document,
    tokenize = 'porter unicode61'
);
"""
