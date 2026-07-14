# MCP Behave Server

This package provides a host-side MCP server for running selected behave scenarios in this repository.

## What it does

The server exposes these MCP tools:

- `list_features`: returns the available feature files under the repository
- `start_behave_scenario`: starts a whitelisted behave scenario in the background and returns a `job_id`
- `check_scenario_status`: checks job status and returns running output tail or compact completion summary
- `get_scenario_logs`: returns a bounded tail of captured stdout logs for a job

It also exposes a health endpoint at `/healthz` for basic checks.

## Local usage

From the repository root, the package can be run directly with uvx without needing to change into the package directory:

```bash
uvx --from $(pwd)/tools/mcp-behave-server mcp-behave-server
```

If you are already inside the package directory, this also works:

```bash
uv run mcp-behave-server
```

## Example MCP client configuration

A minimal config entry for an MCP client looks like this:

```json
{
  "mcpServers": {
    "behave": {
      "command": "uvx",
      "args": [
        "--from",
        "/path/to/repo/tools/mcp-behave-server",
        "mcp-behave-server"
      ]
    }
  }
}
```

If you prefer keeping cache enabled, clear stale entries after package changes:

```bash
uv cache clean mcp-behave-server
```

## Testing

Run the package tests:

```bash
uv run pytest -q
```

Run only fast tests (exclude end-to-end long-running test):

```bash
uv run pytest -q -m "not e2e"
```

Run only the long-running end-to-end MCP test:

```bash
uv run pytest -q -m "e2e and long_running"
```

## Environment variables

The server will forward a small allowlist of environment variables to the behave subprocess when present:

- `UACLIENT_BEHAVE_CONTRACT_TOKEN`
- `UACLIENT_BEHAVE_INSTALL_FROM`
