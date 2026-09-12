# AGENTS.md -- tools/mcp_server_behave

This subproject is a standalone `uv`-managed project with its own
`pyproject.toml`, virtualenv, and CI job, separate from the rest of the repo.
It ships two top-level packages:

- `behave_mcp` -- a host-side MCP server for running behave scenarios.
- `behave_campaign` -- the durable record of test units and attempts a campaign runs
  through. See [behave_campaign/README.md](behave_campaign/README.md).

`behave_campaign` imports `behave_mcp.parser`; nothing in `behave_mcp`
imports `behave_campaign` except the tool wrappers in `server.py`. Keep that direction so the
module graph stays acyclic and `behave_campaign` remains extractable.

The root [AGENTS.md](../../AGENTS.md) still applies (terminology, safety
rules), **except**: this package targets modern Python (>=3.10) and is not
constrained by the root's Python 3.5/Xenial compatibility requirement.

## Architecture

Hexagonal/ports-and-adapters, one module per layer. `behave_campaign` follows
the same layering; its modules are listed in its own README.

`behave_mcp`:

- `domain.py` -- pure logic (command building, validation, report summarizing).
  No I/O, no framework imports.
- `ports.py` -- `Protocol` interfaces the domain/service layer depends on
  (`ProcessLauncher`, job registry, artifact store, workspace).
- `adapters.py` -- concrete implementations of those ports (`subprocess.Popen`,
  filesystem, in-memory registry). Tests inject fakes instead of these.
- `service.py` -- `BehaveService`, orchestrates domain + ports into the actual
  tool behaviors. This is where most business logic changes belong.
- `server.py` -- the FastMCP tool-decorated wrappers; thin, just
  parses/serializes and calls into `_service`.
- `config.py` -- startup env var parsing into a validated `Settings` dataclass.
- `messages.py` -- pydantic DTOs returned across the MCP boundary.

When adding behavior, prefer changing `domain.py`/`service.py` over `server.py`.

## Build, test, lint

```bash
uv sync --extra test          # or --extra lint
uv run pytest -q -m "not e2e" # fast tests; e2e need real LXD + a contract token
uv run black --check behave_mcp behave_campaign tests
uv run isort --check-only behave_mcp behave_campaign tests
uv run flake8 behave_mcp behave_campaign tests
uv run mypy behave_mcp behave_campaign
```

CI (`.github/workflows/mcp-server-behave.yaml`) runs these on changes under
this path only.

## Test conventions

`tests/` is laid out so it is obvious what a failure implicates:

- `tests/behave_mcp/` -- unit tests for that package's layers, one file per
  layer (`test_domain.py`, `test_adapters.py`, `test_service.py`, ...).
  `test_service.py` exercises `BehaveService` directly (real adapters, fakes
  for the launcher/workspace), not the MCP tool layer. `test_golden.py` holds
  byte-shape tests for on-disk serialization; treat failures there as a
  deliberate format change, not a bug to silence.
- `tests/behave_campaign/` -- the same, for that package.
- `tests/shared/` -- code both packages depend on. `parser.py` lives in
  `behave_mcp` but `behave_campaign` builds every campaign plan with it, so a
  parser change breaks both and its tests sit apart from either.
- `tests/mcp_surface/` -- `server.py`'s tool wrappers over the real MCP
  protocol, spanning both packages. Two e2e tests there run real
  subprocesses: `test_e2e.py` needs LXD and a contract token, while
  `test_campaign_e2e.py` needs neither -- it drives a real campaign through
  the real scheduler over `features/_version.feature`, whose scenarios skip
  without `check_version` config, so it proves concurrency without
  provisioning anything. The rest run in process.

Run the campaign e2e when changing the scheduler, the lane runner, or
anything about how jobs are started; it is the only test where behave
subprocesses really run at once:

```bash
uv run pytest -q -m "e2e and long_running" \
  tests/mcp_surface/test_campaign_e2e.py
```

Each directory is a package (`__init__.py`), so the two `test_domain.py` files
coexist without a naming dance, and `tests` itself is one so nothing there can
shadow an installed module.

Shared fixtures and test doubles live in `tests/conftest.py`
(`make_repo_with_feature`, `FakeWorkspace`, `FakeProcess`, `result_json`,
`result_error_text`), imported as `from tests.conftest import ...`. Reuse them
instead of adding another per-file copy.

## Docs

`README.md` is the source of truth for the tool surface, env vars, and safety
constraints. Update it alongside any change to tool signatures, config, or
defaults.

## Code conventions

- DO NOT write long comments explaining behavior. Behavior should be obvious from the code. If behavior is not obvious, raise it to the user.
- NEVER include historical decisions/narrative in code as comments or docstrings. Code should ONLY describe the current state.
- ALWAYS make commit messages concise, and use conventional commit format.

## Project status

This project is currently WIP with no consumers. Breaking changes are OK, just flag them to the user. Breaking changes must be justified with an improvement in some aspect like maintainability, feature surface, duplication, etc.
