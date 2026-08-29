# behave-test-catalog

A standalone, read-optimized catalog of ubuntu-pro-client's `behave`
feature files, backed by SQLite. It answers "which scenarios exist, and
which `(release, machine_type)` combos do they run on" without grepping
`.feature` files, and supports composable filtering plus full-text search
over scenario names/tags/step bodies.

Combo derivation is structural (the same `Examples:` table / step-text
convention the suite already uses), not tag-driven -- the catalog only
ever advertises a combo that `behave -D releases=... -D machine_types=...`
can actually run.

## Setup

This tool is a member of the `tools/` uv workspace, which shares one
virtual environment and lockfile across every modern (dependency-having)
tool in `tools/`. From `tools/`:

```
uv sync --all-extras
```

## CLI

```
# Build (or rebuild) the catalog database.
uv run --project test_catalog test-catalog build --repo-root /path/to/ubuntu-pro-client

# List features, optionally filtered.
uv run --project test_catalog test-catalog features --release jammy --machine-type lxd-container

# Distinct releases/machine_types with scenario counts.
uv run --project test_catalog test-catalog dimensions

# Composable query: every filter given is AND-ed together. releases and
# machine_types require a single combo row to satisfy both.
uv run --project test_catalog test-catalog query --release jammy --machine-type lxd-container

# Full-text keyword search over scenario name/tags/step bodies.
uv run --project test_catalog test-catalog search apt --format json
```

(Run from inside `tools/test_catalog/` instead, and drop `--project
test_catalog` from every command, if that's more convenient.)

All commands accept `--repo-root` (auto-detected by walking up from the
current directory looking for a `features/` directory, if omitted) and
`--db` (defaults to `<repo-root>/.behave_test_catalog.sqlite3`). Every
command except `build` rebuilds the database automatically if any
`.feature` file has changed since it was last built.

## Library

```python
from pathlib import Path
from behave_catalog.db import TestCatalog

with TestCatalog.open_or_build(Path("."), Path(".behave_test_catalog.sqlite3")) as catalog:
    catalog.dimensions()
    catalog.features(tag="security")
    catalog.query(releases=["jammy"], machine_types=["lxd-container"], text="apt")
    catalog.search("security")
```

## Tests

```
uv run --directory test_catalog pytest
```

Use `--directory`, not `--project`, for pytest specifically: `--project`
only picks which project's dependencies/venv to use, it doesn't change
the working directory, so pytest's own config search (which walks
upward from cwd, not from this file) would otherwise skip straight past
this directory's `pyproject.toml` and pick up the outer repo's
`tox.ini` `[pytest]` section instead -- silently dropping this
project's `addopts`/`markers` (e.g. `integration` tests wouldn't be
excluded from a default run). `--directory` genuinely `cd`s first, so
config resolution lands here. Run integration tests (which exercise the
real `features/*.feature` corpus, excluded by default) with:

```
uv run --directory test_catalog pytest -m integration
```
