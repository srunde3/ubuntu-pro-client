# MCP Behave Server

This package provides a host-side MCP server for running selected behave scenarios in this repository.

## What it does

The server exposes two MCP tools:

- `list_features`: returns the available feature files under the repository
- `run_behave_scenario`: runs a whitelisted behave scenario through `tox -e behave`

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

## Environment variables

The server will forward a small allowlist of environment variables to the behave subprocess when present:

- `UACLIENT_BEHAVE_CONTRACT_TOKEN`
- `UACLIENT_BEHAVE_INSTALL_FROM`

## Current scope

This initial version is intentionally narrow:

- only the attach feature is currently allowed
- the server runs locally on the host side
- the focus is on proving the MCP workflow with a local agent first

## Future work

Planned follow-up work includes:

- broader feature allowlisting
- support for additional behave scenarios and parameters
- broader agent integration options
- tighter auth and deployment boundaries for hosted environments
