"""Process-level startup behavior: config must fail loudly but cleanly.

These spawn a real subprocess because the failure happens at module import
time, before any test could exercise it via an already-imported module.
"""

import os
import subprocess
import sys


def _run_import(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, "-c", "from behave_mcp import server"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_invalid_config_exits_nonzero_with_clean_message():
    result = _run_import({"MCP_PORT": "notanumber"})

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "mcp-server-behave: invalid configuration: "
        "MCP_PORT must be a valid port number, got 'notanumber'"
    )
    assert "Traceback" not in result.stderr


def test_valid_config_imports_cleanly():
    result = _run_import({"MCP_TRANSPORT": "stdio"})

    assert result.returncode == 0
    assert result.stderr == ""
