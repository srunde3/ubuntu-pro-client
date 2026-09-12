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

Shared fixtures/test doubles live in `tests/conftest.py`
(`make_repo_with_feature`, `FakeWorkspace`, `FakeProcess`, `result_json`,
`result_error_text`). Reuse them instead of adding another per-file copy.

- `test_domain.py`, `test_adapters.py` -- unit tests for those layers directly.
- `test_service.py` -- exercises `BehaveService` directly (real adapters +
  fakes for the launcher/workspace), not the MCP tool layer.
- `test_mcp_integration.py`, `test_mcp_e2e.py` -- exercise `server.py`'s actual
  tool wrappers over the real MCP protocol (in-process / real subprocess).
- `test_golden.py` -- byte-shape tests for on-disk serialization; treat
  failures here as a deliberate format change, not a bug to silence.
- `test_campaign_*.py` -- unit tests for the `behave_campaign` package's layers. The
  `campaign_` prefix is what keeps them from colliding with the `behave_mcp`
  test module of the same layer name (`test_domain.py`).

## Docs

`README.md` is the source of truth for the tool surface, env vars, and safety
constraints. Update it alongside any change to tool signatures, config, or
defaults.
