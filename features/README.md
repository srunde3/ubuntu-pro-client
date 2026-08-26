# `features/` package

This dir holds the `behave` integration suite (`*.feature`, `steps/`,
`environment.py`) **and** a dev-tooling package, `pro-client-features`.

[`behave_features.py`](behave_features.py) parses feature files into typed data
(mainly `<release>`/`<machine_type>` combos and `uses.config.*` requirements),
and is consumed by `tools/coverage_gaps.py` and `tools/mcp-behave-server`. Keep
it in sync when feature-test conventions change.

The nested `pyproject.toml` is **dev/CI-only** -- not part of the shipped client
(built from the repo-root `setup.py`), so it targets Python 3.10+ rather than
the client's 3.5/Xenial floor. It also re-pins `black`/`isort` to 79 chars to
keep consistent formatting conventions.

```sh
# from features/
uv sync --extra test           # creates a git-ignored .venv/
uv run pytest                  # unit tests
uv run pytest -m integration   # against the real features/*.feature corpus
```
