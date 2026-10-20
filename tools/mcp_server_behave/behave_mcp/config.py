"""Startup configuration parsing for the behave MCP server.

Settings are parsed and validated once, when the server starts. Invalid
values raise ``ConfigError`` loudly rather than degrading at request time.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from behave_mcp.domain.pools import (
    DEFAULT_POOL_LIMITS,
    POOL_LIMIT_CAPS,
    POOL_LIMIT_ENV_VARS,
    Pool,
)

HOST_ENV_VAR = "MCP_HOST"
PORT_ENV_VAR = "MCP_PORT"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000

# What to do instead, for each variable the server no longer reads.
REMOVED_ENV_VARS: dict[str, str] = {
    "MCP_MAX_PARALLEL_JOBS": (
        "use MCP_MAX_LXD_JOBS, MCP_MAX_CLOUD_JOBS and MCP_MAX_WSL_JOBS"
    ),
    "MCP_ALLOW_CLOUD_MACHINE_TYPES": "use MCP_MAX_CLOUD_JOBS",
    "MCP_TRANSPORT": (
        "the server always serves streamable HTTP on MCP_HOST and MCP_PORT"
    ),
}


CAMPAIGN_POLL_TIMEOUT_ENV_VAR = "MCP_CAMPAIGN_POLL_TIMEOUT"


# How long await_campaign_events may hold a request open. The real ceiling
# is the client's own request timeout, which varies per host and is not ours
# to set, so this is configurable and capped conservatively.
DEFAULT_CAMPAIGN_POLL_TIMEOUT = 60


MAX_CAMPAIGN_POLL_TIMEOUT = 120


class ConfigError(Exception):
    """Raised when environment configuration is invalid at startup."""


@dataclass(frozen=True)
class Settings:
    """Validated server settings parsed once at startup."""

    pool_limits: Mapping[Pool, int]
    campaign_poll_timeout: int
    host: str
    port: int


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Parse and validate settings from ``environ``.

    Raises ConfigError if any value is present but unusable.
    """
    _reject_removed(environ)
    return Settings(
        pool_limits={pool: _parse_pool_limit(environ, pool) for pool in Pool},
        campaign_poll_timeout=_parse_campaign_poll_timeout(environ),
        host=environ.get(HOST_ENV_VAR, "").strip() or _DEFAULT_HOST,
        port=_parse_port(environ),
    )


def _reject_removed(environ: Mapping[str, str]) -> None:
    for name, instead in REMOVED_ENV_VARS.items():
        if environ.get(name, "").strip():
            raise ConfigError(f"{name} is no longer read: {instead}")


def _parse_pool_limit(environ: Mapping[str, str], pool: Pool) -> int:
    name = POOL_LIMIT_ENV_VARS[pool]
    raw = environ.get(name, "").strip()
    if not raw:
        return DEFAULT_POOL_LIMITS[pool]

    cap = POOL_LIMIT_CAPS.get(pool)
    expected = (
        f"an integer between 0 and {cap}"
        if cap is not None
        else "a non-negative integer"
    )
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be {expected}, got {raw!r}")

    if value < 0 or (cap is not None and value > cap):
        raise ConfigError(f"{name} must be {expected}, got {raw!r}")

    return value


def _parse_campaign_poll_timeout(environ: Mapping[str, str]) -> int:
    raw = environ.get(CAMPAIGN_POLL_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_CAMPAIGN_POLL_TIMEOUT

    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{CAMPAIGN_POLL_TIMEOUT_ENV_VAR} must be an integer "
            f"between 1 and {MAX_CAMPAIGN_POLL_TIMEOUT}, got {raw!r}"
        )

    if not 0 < value <= MAX_CAMPAIGN_POLL_TIMEOUT:
        raise ConfigError(
            f"{CAMPAIGN_POLL_TIMEOUT_ENV_VAR} must be an integer "
            f"between 1 and {MAX_CAMPAIGN_POLL_TIMEOUT}, got {raw!r}"
        )

    return value


def _parse_port(environ: Mapping[str, str]) -> int:
    raw = environ.get(PORT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_PORT

    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{PORT_ENV_VAR} must be a valid port number, got {raw!r}"
        )

    if not 0 < value < 65536:
        raise ConfigError(
            f"{PORT_ENV_VAR} must be a valid port number, got {raw!r}"
        )

    return value
